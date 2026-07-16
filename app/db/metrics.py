from __future__ import annotations

import hashlib
import hmac
import sqlite3
import threading
from collections import defaultdict
from contextlib import closing
from datetime import timedelta
from zoneinfo import ZoneInfo

from app.config import settings
from app.db.jobs import utc_now


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

    def snapshot(self) -> dict[str, object]:
        self.initialize()
        today = self._day()
        start = utc_now() - timedelta(days=6)
        days = [
            (start + timedelta(days=offset)).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
            for offset in range(7)
        ]
        today_start = utc_now().astimezone(ZoneInfo("Asia/Shanghai")).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(ZoneInfo("UTC")).isoformat()
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

        daily_by_day: dict[str, dict[str, int]] = defaultdict(lambda: {"page_views": 0, "unique_visitors": 0})
        for day, page_views, unique_visitors in daily_rows:
            daily_by_day[day] = {"page_views": page_views, "unique_visitors": unique_visitors}
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
