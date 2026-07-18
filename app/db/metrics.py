from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import threading
from collections import defaultdict
from contextlib import closing
from datetime import timedelta
from zoneinfo import ZoneInfo

from app.config import settings
from app.db.jobs import utc_now


BOT_USER_AGENT = re.compile(
    r"bot|crawler|spider|slurp|curl|wget|python|headless|lighthouse|facebookexternalhit|preview",
    re.IGNORECASE,
)


class MetricsStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(settings.database_path, timeout=30, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @staticmethod
    def _day() -> str:
        return utc_now().astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()

    def initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            with closing(self._connect()) as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS page_view_days (
                        day TEXT PRIMARY KEY,
                        page_views INTEGER NOT NULL DEFAULT 0,
                        unique_visitors INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS traffic_sessions (
                        session_id TEXT PRIMARY KEY,
                        day TEXT NOT NULL,
                        first_seen TEXT NOT NULL,
                        last_seen TEXT NOT NULL,
                        page_views INTEGER NOT NULL DEFAULT 0,
                        suspected_bot INTEGER NOT NULL DEFAULT 0,
                        engaged_at TEXT,
                        engagement_seconds INTEGER NOT NULL DEFAULT 0,
                        free_submissions INTEGER NOT NULL DEFAULT 0,
                        batch_submissions INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_traffic_sessions_day ON traffic_sessions(day)"
                )
                columns = {row[1] for row in connection.execute("PRAGMA table_info(traffic_sessions)")}
                for name, kind in (
                    ("engagement_seconds", "INTEGER NOT NULL DEFAULT 0"),
                    ("free_submissions", "INTEGER NOT NULL DEFAULT 0"),
                    ("batch_submissions", "INTEGER NOT NULL DEFAULT 0"),
                ):
                    if name not in columns:
                        connection.execute(f"ALTER TABLE traffic_sessions ADD COLUMN {name} {kind}")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS anonymous_free_usage (
                        network_hash TEXT NOT NULL,
                        period TEXT NOT NULL,
                        count INTEGER NOT NULL DEFAULT 0,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(network_hash, period)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS daily_visitors (
                        day TEXT NOT NULL,
                        visitor_hash TEXT NOT NULL,
                        PRIMARY KEY(day, visitor_hash)
                    )
                    """
                )
            self._initialized = True

    def record_page_view(self, client_host: str, user_agent: str) -> None:
        self.initialize()
        day = self._day()
        material = f"{day}|{client_host}|{user_agent}".encode("utf-8")
        key = (settings.metrics_salt or "verigo-metrics-unconfigured").encode("utf-8")
        visitor_hash = hmac.new(key, material, hashlib.sha256).hexdigest()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            is_new = connection.execute(
                "INSERT OR IGNORE INTO daily_visitors(day, visitor_hash) VALUES (?, ?)",
                (day, visitor_hash),
            ).rowcount
            connection.execute(
                """
                INSERT INTO page_view_days(day, page_views, unique_visitors) VALUES (?, 1, ?)
                ON CONFLICT(day) DO UPDATE SET
                    page_views=page_view_days.page_views+1,
                    unique_visitors=page_view_days.unique_visitors+excluded.unique_visitors
                """,
                (day, is_new),
            )
            connection.commit()

    def record_session_page_view(self, session_id: str, user_agent: str) -> None:
        self.initialize()
        now = utc_now().isoformat()
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO traffic_sessions(
                    session_id, day, first_seen, last_seen, page_views, suspected_bot
                ) VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    last_seen=excluded.last_seen, page_views=traffic_sessions.page_views+1
                """,
                (session_id, self._day(), now, now, int(not user_agent or bool(BOT_USER_AGENT.search(user_agent)))),
            )

    def session_is_active(self, session_id: str) -> bool:
        self.initialize()
        cutoff = (utc_now() - timedelta(minutes=30)).isoformat()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM traffic_sessions WHERE session_id=? AND last_seen >= ?",
                (session_id, cutoff),
            ).fetchone()
        return row is not None

    def record_engagement(self, session_id: str, seconds: int = 0) -> None:
        self.initialize()
        seconds = max(0, min(seconds, 1800))
        now = utc_now().isoformat()
        with closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE traffic_sessions SET engaged_at=COALESCE(engaged_at, ?),
                    engagement_seconds=MAX(engagement_seconds, ?), last_seen=?
                WHERE session_id=?
                """,
                (now, seconds, now, session_id),
            )

    def record_conversion(self, session_id: str | None, kind: str) -> None:
        if not session_id or kind not in {"free", "batch"}:
            return
        self.initialize()
        column = "free_submissions" if kind == "free" else "batch_submissions"
        with closing(self._connect()) as connection:
            connection.execute(
                f"UPDATE traffic_sessions SET {column}={column}+1, last_seen=? WHERE session_id=?",
                (utc_now().isoformat(), session_id),
            )

    def reserve_free_single(self, network_hash: str, limit: int) -> None:
        self.initialize()
        if limit < 1:
            raise ValueError("免费单个验证暂不可用")
        period = self._day()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT count FROM anonymous_free_usage WHERE network_hash=? AND period=?",
                (network_hash, period),
            ).fetchone()
            if row and int(row[0]) >= limit:
                connection.rollback()
                raise ValueError("免费验证次数已达今日上限，请明日再试")
            connection.execute(
                """
                INSERT INTO anonymous_free_usage(network_hash, period, count, updated_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(network_hash, period) DO UPDATE SET
                    count=anonymous_free_usage.count+1, updated_at=excluded.updated_at
                """,
                (network_hash, period, utc_now().isoformat()),
            )
            connection.commit()

    def release_free_single(self, network_hash: str) -> None:
        self.initialize()
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE anonymous_free_usage SET count=MAX(count-1, 0), updated_at=? WHERE network_hash=? AND period=?",
                (utc_now().isoformat(), network_hash, self._day()),
            )

    def snapshot(self) -> dict[str, object]:
        self.initialize()
        today = self._day()
        start = utc_now() - timedelta(days=13)
        days = [
            (start + timedelta(days=offset)).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
            for offset in range(14)
        ]
        today_start = utc_now().astimezone(ZoneInfo("Asia/Shanghai")).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(ZoneInfo("UTC")).isoformat()
        bounce_cutoff = (utc_now() - timedelta(minutes=30)).isoformat()
        with closing(self._connect()) as connection:
            today_views = connection.execute(
                "SELECT page_views, unique_visitors FROM page_view_days WHERE day=?", (today,)
            ).fetchone() or (0, 0)
            totals = connection.execute(
                "SELECT COALESCE(SUM(page_views), 0), COALESCE(SUM(unique_visitors), 0) FROM page_view_days"
            ).fetchone()
            users_total = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            users_today = connection.execute(
                "SELECT COUNT(*) FROM users WHERE created_at >= ?", (today_start,)
            ).fetchone()[0]
            verified_today = connection.execute(
                "SELECT COUNT(*) FROM credit_ledger WHERE kind='email_verified' AND created_at >= ?",
                (today_start,),
            ).fetchone()[0]
            verified_users = connection.execute(
                "SELECT COUNT(*) FROM users WHERE email_verified=1"
            ).fetchone()[0]
            jobs_total = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            jobs_today = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE created_at >= ?", (today_start,)
            ).fetchone()[0]
            job_statuses = dict(connection.execute(
                "SELECT status, COUNT(*) FROM jobs GROUP BY status"
            ).fetchall())
            today_job_health = connection.execute(
                """
                SELECT COUNT(*),
                    COALESCE(SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END), 0),
                    COALESCE(AVG(CASE WHEN status='completed' AND started_at IS NOT NULL AND finished_at IS NOT NULL
                        THEN (julianday(finished_at)-julianday(started_at))*86400 END), 0)
                FROM jobs WHERE created_at >= ?
                """,
                (today_start,),
            ).fetchone()
            result_rows = connection.execute(
                "SELECT results_json FROM jobs WHERE status='completed' AND finished_at >= ?",
                (today_start,),
            ).fetchall()
            credits_today = connection.execute(
                """
                SELECT COALESCE(SUM(-delta), 0) FROM credit_ledger
                WHERE kind='verification' AND created_at >= ?
                """,
                (today_start,),
            ).fetchone()[0]
            revenue_today = connection.execute(
                "SELECT COALESCE(SUM(amount_fen), 0), COUNT(*) FROM payment_orders WHERE status='paid' AND paid_at >= ?",
                (today_start,),
            ).fetchone()
            revenue_total = connection.execute(
                "SELECT COALESCE(SUM(amount_fen), 0), COUNT(*) FROM payment_orders WHERE status='paid'"
            ).fetchone()
            daily_rows = connection.execute(
                "SELECT day, page_views, unique_visitors FROM page_view_days WHERE day >= ?",
                (days[0],),
            ).fetchall()
            traffic_rows = connection.execute(
                """
                SELECT day, COUNT(*),
                    COALESCE(SUM(suspected_bot), 0),
                    COALESCE(SUM(CASE WHEN suspected_bot=0 AND engaged_at IS NOT NULL THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN suspected_bot=0 AND engaged_at IS NULL AND first_seen <= ? THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN suspected_bot=0 AND (engaged_at IS NOT NULL OR first_seen <= ?) THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN suspected_bot=0 THEN engagement_seconds ELSE 0 END), 0),
                    COALESCE(SUM(free_submissions), 0), COALESCE(SUM(batch_submissions), 0)
                FROM traffic_sessions WHERE day >= ? GROUP BY day
                """,
                (bounce_cutoff, bounce_cutoff, days[0]),
            ).fetchall()

        daily_by_day: dict[str, dict[str, int]] = defaultdict(lambda: {"page_views": 0, "unique_visitors": 0})
        for day, page_views, unique_visitors in daily_rows:
            daily_by_day[day] = {"page_views": page_views, "unique_visitors": unique_visitors}
        traffic_by_day = {
            day: {
                "sessions": sessions, "suspected_bots": bots, "engaged_sessions": engaged,
                "bounced_sessions": bounced, "eligible_sessions": eligible, "engagement_seconds": seconds,
                "free_submissions": free_submissions, "batch_submissions": batch_submissions,
            }
            for day, sessions, bots, engaged, bounced, eligible, seconds, free_submissions, batch_submissions in traffic_rows
        }
        for day in days:
            traffic = traffic_by_day.get(day, {
                "sessions": 0, "suspected_bots": 0, "engaged_sessions": 0, "bounced_sessions": 0, "eligible_sessions": 0,
                "engagement_seconds": 0, "free_submissions": 0, "batch_submissions": 0,
            })
            non_bot_sessions = traffic["sessions"] - traffic["suspected_bots"]
            traffic["bounce_rate"] = round(traffic["bounced_sessions"] * 100 / traffic["eligible_sessions"], 1) if traffic["eligible_sessions"] else 0
            traffic["bot_rate"] = round(traffic["suspected_bots"] * 100 / traffic["sessions"], 1) if traffic["sessions"] else 0
            traffic["average_engagement_seconds"] = round(traffic["engagement_seconds"] / traffic["engaged_sessions"]) if traffic["engaged_sessions"] else 0
            daily_by_day[day].update(traffic)
        today_traffic = daily_by_day[today]
        deliverable = 0
        results_total = 0
        for (results_json,) in result_rows:
            try:
                results = json.loads(results_json)
            except (TypeError, json.JSONDecodeError):
                continue
            results_total += len(results)
            deliverable += sum(item.get("deliverable") is True for item in results)
        completed_today = int(today_job_health[1])
        failed_today = int(today_job_health[2])
        settled_today = completed_today + failed_today
        return {
            "updated_at": utc_now().isoformat(),
            "today": {
                "page_views": today_views[0],
                "unique_visitors": today_views[1],
                "new_users": users_today,
                "new_jobs": jobs_today,
                "credits_consumed": credits_today,
                "revenue_fen": revenue_today[0],
                "paid_orders": revenue_today[1],
                "sessions": today_traffic["sessions"],
                "suspected_bots": today_traffic["suspected_bots"],
                "engaged_sessions": today_traffic["engaged_sessions"],
                "bounce_rate": today_traffic["bounce_rate"],
                "bot_rate": today_traffic["bot_rate"],
                "average_engagement_seconds": today_traffic["average_engagement_seconds"],
                "free_submissions": today_traffic["free_submissions"],
                "batch_submissions": today_traffic["batch_submissions"],
                "free_conversion_rate": round(today_traffic["free_submissions"] * 100 / (today_traffic["sessions"] - today_traffic["suspected_bots"]), 1) if today_traffic["sessions"] > today_traffic["suspected_bots"] else 0,
                "batch_conversion_rate": round(today_traffic["batch_submissions"] * 100 / (today_traffic["sessions"] - today_traffic["suspected_bots"]), 1) if today_traffic["sessions"] > today_traffic["suspected_bots"] else 0,
                "verified_users": verified_today,
                "job_completion_rate": round(completed_today * 100 / settled_today, 1) if settled_today else 0,
                "average_job_seconds": round(float(today_job_health[3])),
                "deliverable_rate": round(deliverable * 100 / results_total, 1) if results_total else 0,
                "results_processed": results_total,
            },
            "totals": {
                "page_views": totals[0],
                "unique_visitors": totals[1],
                "users": users_total,
                "verified_users": verified_users,
                "jobs": jobs_total,
                "revenue_fen": revenue_total[0],
                "paid_orders": revenue_total[1],
            },
            "jobs": {status: int(job_statuses.get(status, 0)) for status in ("queued", "running", "completed", "failed")},
            "daily": [{"day": day, **daily_by_day[day]} for day in days],
        }


metrics_store = MetricsStore()
