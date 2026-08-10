"""Durable account rotation for Google Cloud Shell workers.

Workers still pull leases independently, but this coordinator gates each Gmail
claim so the account with the lowest usage for the current UTC day is selected
first. Reservations close the race between several polling workers.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import settings
from app.db.sqlite import begin_immediate, connect

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _day(now: datetime | None = None) -> str:
    return (now or _now()).date().isoformat()


class CloudShellCoordinator:
    """Coordinate Cloud Shell claims without storing credentials or tokens."""

    def __init__(self, database_path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._database_path = database_path or settings.database_path
        self._pool_refreshed_at = 0.0

    def _connect(self):
        return connect(self._database_path)

    @property
    def enabled(self) -> bool:
        return bool(getattr(settings, "cloudshell_coordinator_enabled", True))

    def initialize(self) -> None:
        with self._lock, closing(self._connect()) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS cloudshell_account_usage (
                    worker_id TEXT NOT NULL,
                    usage_date TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    claimed_units INTEGER NOT NULL DEFAULT 0,
                    claimed_tasks INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    cooldown_until TEXT,
                    last_claimed_at TEXT,
                    last_failure_at TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    soft_quota_units INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(worker_id, usage_date)
                )"""
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(cloudshell_account_usage)")}
            for name, kind in (
                ("active", "INTEGER NOT NULL DEFAULT 1"),
                ("soft_quota_units", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE cloudshell_account_usage ADD COLUMN {name} {kind}")
            db.execute(
                """CREATE TABLE IF NOT EXISTS cloudshell_claim_reservations (
                    token TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    usage_date TEXT NOT NULL,
                    reserved_units INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_cloudshell_usage_day "
                "ON cloudshell_account_usage(usage_date, enabled, claimed_units)"
            )
            # The main job store normally creates this table first; keeping a
            # small compatible table here makes the coordinator independently
            # testable and safe during first boot.
            db.execute(
                """CREATE TABLE IF NOT EXISTS worker_nodes (
                    target TEXT NOT NULL, worker_id TEXT NOT NULL,
                    capacity INTEGER NOT NULL DEFAULT 1,
                    health TEXT NOT NULL DEFAULT 'healthy',
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY(target, worker_id)
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, execution_target TEXT NOT NULL DEFAULT 'local',
                    status TEXT NOT NULL DEFAULT 'queued'
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS job_results (
                    job_id TEXT NOT NULL, progress_state TEXT NOT NULL DEFAULT 'pending'
                )"""
            )
            db.commit()

    def sync_accounts(self, accounts: list[dict[str, Any]] | None = None) -> None:
        """Register built-in and manifest workers for today's rotation pool."""
        self.initialize()
        if accounts is None:
            accounts = self._manifest_accounts()
        builtins = [
            {
                "account_id": "account1",
                "worker_id": "cloudshell-gmail-1",
                "enabled": bool(settings.google_cloudshell_enabled),
            },
            {
                "account_id": "account2",
                "worker_id": "cloudshell-gmail-2",
                "enabled": bool(settings.google_cloudshell_secondary_enabled),
            },
        ]
        records = builtins + [dict(item) for item in accounts]
        today = _day()
        with self._lock, closing(self._connect()) as db:
            begin_immediate(db)
            for item in records:
                worker_id = str(item.get("worker_id") or "").strip()
                if not worker_id:
                    continue
                account_id = str(item.get("account_id") or item.get("id") or worker_id).strip()
                db.execute(
                    """INSERT INTO cloudshell_account_usage
                        (worker_id, usage_date, account_id, enabled, soft_quota_units)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(worker_id, usage_date) DO UPDATE SET
                            account_id=excluded.account_id,
                            enabled=excluded.enabled,
                            soft_quota_units=excluded.soft_quota_units""",
                    (
                        worker_id,
                        today,
                        account_id,
                        int(bool(item.get("enabled", True))),
                        max(0, int(item.get("soft_quota_units", settings.cloudshell_soft_quota_units) or 0)),
                    ),
                )
            db.commit()

    @staticmethod
    def _manifest_accounts() -> list[dict[str, Any]]:
        path = Path(str(settings.google_cloudshell_accounts_file or ""))
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        raw = payload.get("accounts") if isinstance(payload, dict) else payload
        return [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    def _ensure_worker(self, db: Any, worker_id: str, today: str) -> None:
        row = db.execute(
            "SELECT 1 FROM cloudshell_account_usage WHERE worker_id=? AND usage_date=?",
            (worker_id, today),
        ).fetchone()
        if row is None:
            # Unknown workers remain usable for backwards compatibility and
            # smoke tests; production workers should be declared in the manifest.
            db.execute(
                """INSERT INTO cloudshell_account_usage
                    (worker_id, usage_date, account_id, enabled) VALUES (?, ?, ?, 1)""",
                (worker_id, today, worker_id),
            )

    @staticmethod
    def _canonical_worker_id(db: Any, worker_id: str, today: str) -> str:
        """Group lifecycle child processes (``account3-1``) under one account."""
        row = db.execute(
            "SELECT worker_id FROM cloudshell_account_usage WHERE usage_date=? AND worker_id=?",
            (today, worker_id),
        ).fetchone()
        if row:
            return worker_id
        base = worker_id.rsplit("-", 1)[0] if worker_id.rsplit("-", 1)[-1].isdigit() else ""
        if base:
            row = db.execute(
                "SELECT worker_id FROM cloudshell_account_usage WHERE usage_date=? AND worker_id=?",
                (today, base),
            ).fetchone()
            if row:
                return base
        return worker_id

    def refresh_pool(self, *, force: bool = False) -> None:
        """Resize the active account pool from queue pressure.

        A short process-local throttle keeps the claim endpoint from rewriting
        the pool on every polling request while still reacting quickly to a
        queue burst. Active accounts are kept ahead of cold accounts to avoid
        needless Cloud Shell wakeups.
        """
        if not self.enabled:
            return
        now_monotonic = time.monotonic()
        if not force and now_monotonic - self._pool_refreshed_at < 5:
            return
        self.sync_accounts()
        with self._lock:
            now_monotonic = time.monotonic()
            if not force and now_monotonic - self._pool_refreshed_at < 5:
                return
            today = _day()
            now = _now()
            with closing(self._connect()) as db:
                begin_immediate(db)
                queue_depth = int(db.execute(
                    """SELECT COUNT(*) FROM jobs
                       WHERE execution_target='gmail' AND status IN ('queued', 'running')
                         AND EXISTS (SELECT 1 FROM job_results r
                                    WHERE r.job_id=jobs.id AND r.progress_state='pending')"""
                ).fetchone()[0])
                desired = max(
                    settings.cloudshell_active_min_accounts,
                    (queue_depth + settings.cloudshell_queue_per_active_account - 1)
                    // settings.cloudshell_queue_per_active_account,
                )
                desired = min(settings.cloudshell_active_max_accounts, desired)
                rows = db.execute(
                    """SELECT worker_id, enabled, claimed_units, claimed_tasks,
                              COALESCE(cooldown_until, ''), soft_quota_units, active
                       FROM cloudshell_account_usage WHERE usage_date=?""",
                    (today,),
                ).fetchall()
                eligible = [
                    row for row in rows
                    if int(row[1])
                    and (not row[4] or row[4] <= now.isoformat())
                    and (not int(row[5]) or int(row[2]) < int(row[5]))
                ]
                # If every account reached its configured soft budget, keep
                # service available and rely on provider errors for cooling.
                if not eligible:
                    eligible = [
                        row for row in rows
                        if int(row[1]) and (not row[4] or row[4] <= now.isoformat())
                    ]
                # Prefer accounts that already have a fresh worker heartbeat.
                # Otherwise a previously active but now-offline account can keep
                # the active slot while a live Cloud Shell worker is prevented
                # from claiming queued work.
                healthy_before = (
                    now - timedelta(seconds=settings.node_stale_seconds)
                ).isoformat()
                healthy_worker_ids = {
                    str(value[0]).rsplit("-", 1)[0]
                    if str(value[0]).rsplit("-", 1)[-1].isdigit()
                    else str(value[0])
                    for value in db.execute(
                        """SELECT worker_id FROM worker_nodes
                           WHERE target='gmail' AND health='healthy'
                             AND last_seen_at >= ?""",
                        (healthy_before,),
                    ).fetchall()
                }
                eligible.sort(key=lambda row: (
                    -int(str(row[0]) in healthy_worker_ids),
                    -int(row[6]), int(row[2]), int(row[3]), row[4] or "", row[0]
                ))
                selected = {str(row[0]) for row in eligible[: min(desired, len(eligible))]}
                db.execute(
                    "UPDATE cloudshell_account_usage SET active=0 WHERE usage_date=?",
                    (today,),
                )
                if selected:
                    placeholders = ",".join("?" for _ in selected)
                    db.execute(
                        f"UPDATE cloudshell_account_usage SET active=1 WHERE usage_date=? "
                        f"AND worker_id IN ({placeholders})",
                        (today, *selected),
                    )
                db.commit()
            self._pool_refreshed_at = now_monotonic

    def account_can_wake(self, account_id: str) -> bool:
        """Return whether a lifecycle may start this account's Cloud Shell."""
        if not self.enabled:
            return True
        self.refresh_pool()
        today = _day()
        with closing(self._connect()) as db:
            row = db.execute(
                """SELECT active, enabled, COALESCE(cooldown_until, ''), claimed_units,
                          soft_quota_units
                   FROM cloudshell_account_usage WHERE usage_date=? AND account_id=?""",
                (today, account_id),
            ).fetchone()
        if not row:
            return False
        return bool(
            int(row[0]) and int(row[1])
            and (not row[2] or row[2] <= _now().isoformat())
        )

    def worker_is_healthy(self, worker_id: str) -> bool:
        """Return whether one account already has a live polling process."""
        self.initialize()
        cutoff = (_now() - timedelta(seconds=settings.node_stale_seconds)).isoformat()
        with closing(self._connect()) as db:
            row = db.execute(
                """SELECT 1 FROM worker_nodes
                   WHERE target='gmail' AND health='healthy' AND last_seen_at >= ?
                     AND (worker_id=? OR worker_id LIKE ?)
                   LIMIT 1""",
                (cutoff, worker_id, f"{worker_id}-%"),
            ).fetchone()
        return row is not None

    def reserve(self, worker_id: str, reserved_units: int) -> str | None:
        """Reserve a claim slot if this worker is currently least-used."""
        if not self.enabled:
            return "disabled"
        # Refresh the manifest and active pool at claim time so queue pressure
        # can wake additional accounts without a process restart.
        self.refresh_pool()
        self.initialize()
        worker_id = worker_id.strip()
        if not worker_id:
            return None
        today = _day()
        reserved_units = max(1, int(reserved_units))
        with self._lock, closing(self._connect()) as db:
            begin_immediate(db)
            worker_id = self._canonical_worker_id(db, worker_id, today)
            self._ensure_worker(db, worker_id, today)
            now = _now()
            db.execute(
                "DELETE FROM cloudshell_claim_reservations WHERE created_at < ?",
                ((now - timedelta(minutes=10)).isoformat(),),
            )
            worker = db.execute(
                """SELECT enabled, COALESCE(cooldown_until, ''), claimed_units
                   FROM cloudshell_account_usage WHERE worker_id=? AND usage_date=?""",
                (worker_id, today),
            ).fetchone()
            if not worker or not int(worker[0]):
                db.commit()
                return None
            if worker[1] and worker[1] > now.isoformat():
                db.commit()
                return None
            online_rows = db.execute(
                """SELECT worker_id FROM worker_nodes
                   WHERE target='gmail' AND health='healthy' AND last_seen_at >= ?""",
                ((now - timedelta(seconds=settings.node_stale_seconds)).isoformat(),),
            ).fetchall()
            online_accounts = {
                str(value[0]).rsplit("-", 1)[0]
                if str(value[0]).rsplit("-", 1)[-1].isdigit()
                else str(value[0])
                for value in online_rows
            }
            rows = db.execute(
                """SELECT u.worker_id, u.claimed_units,
                          COALESCE(SUM(r.reserved_units), 0) AS reserved
                   FROM cloudshell_account_usage u
                   LEFT JOIN cloudshell_claim_reservations r
                     ON r.worker_id=u.worker_id AND r.usage_date=u.usage_date
                   WHERE u.usage_date=? AND u.enabled=1 AND u.active=1
                   GROUP BY u.worker_id, u.claimed_units
                   ORDER BY u.claimed_units + reserved, u.last_claimed_at, u.worker_id""",
                (today,),
            ).fetchall()
            if online_accounts:
                rows = [row for row in rows if str(row[0]) in online_accounts]
            if not rows or str(rows[0][0]) != worker_id:
                db.commit()
                return None
            token = secrets.token_urlsafe(18)
            db.execute(
                "INSERT INTO cloudshell_claim_reservations VALUES (?, ?, ?, ?, ?)",
                (token, worker_id, today, reserved_units, now.isoformat()),
            )
            db.commit()
            return token

    def commit(self, token: str, units: int) -> bool:
        if token == "disabled":
            return True
        self.initialize()
        now = _now()
        with self._lock, closing(self._connect()) as db:
            begin_immediate(db)
            row = db.execute(
                "SELECT worker_id, usage_date FROM cloudshell_claim_reservations WHERE token=?",
                (token,),
            ).fetchone()
            if not row:
                db.commit()
                return False
            worker_id, usage_date = str(row[0]), str(row[1])
            db.execute("DELETE FROM cloudshell_claim_reservations WHERE token=?", (token,))
            db.execute(
                """UPDATE cloudshell_account_usage
                   SET claimed_units=claimed_units+?, claimed_tasks=claimed_tasks+1,
                       last_claimed_at=?, failure_count=0, cooldown_until=NULL
                   WHERE worker_id=? AND usage_date=?""",
                (max(1, int(units)), now.isoformat(), worker_id, usage_date),
            )
            db.commit()
            return True

    def release(self, token: str | None) -> None:
        if not token or token == "disabled":
            return
        self.initialize()
        with self._lock, closing(self._connect()) as db:
            db.execute("DELETE FROM cloudshell_claim_reservations WHERE token=?", (token,))
            db.commit()

    def record_failure(self, worker_id: str, error: str) -> None:
        detail = error.lower()
        if not any(token in detail for token in ("quota", "resource_exhausted", "weekly", "rate limit")):
            return
        self.initialize()
        now = _now()
        cooldown = now + timedelta(seconds=settings.cloudshell_quota_cooldown_seconds)
        with self._lock, closing(self._connect()) as db:
            begin_immediate(db)
            today = _day(now)
            worker_id = self._canonical_worker_id(db, worker_id, today)
            self._ensure_worker(db, worker_id, today)
            db.execute(
                """UPDATE cloudshell_account_usage
                   SET failure_count=failure_count+1, cooldown_until=?, last_failure_at=?
                   WHERE worker_id=? AND usage_date=?""",
                (cooldown.isoformat(), now.isoformat(), worker_id, today),
            )
            db.commit()

    def snapshot(self) -> list[dict[str, Any]]:
        self.sync_accounts()
        self.initialize()
        now = _now()
        stale_before = (now - timedelta(seconds=settings.node_stale_seconds)).isoformat()
        with closing(self._connect()) as db:
            rows = db.execute(
                """SELECT account_id, worker_id, enabled, active, usage_date, claimed_units,
                          claimed_tasks, failure_count, cooldown_until, last_claimed_at,
                          soft_quota_units
                   FROM cloudshell_account_usage WHERE usage_date=?
                   ORDER BY claimed_units, last_claimed_at, worker_id""",
                (_day(),),
            ).fetchall()
            queue_depth = int(db.execute(
                """SELECT COUNT(*) FROM jobs
                   WHERE execution_target='gmail' AND status IN ('queued', 'running')
                     AND EXISTS (SELECT 1 FROM job_results r WHERE r.job_id=jobs.id
                                AND r.progress_state='pending')"""
            ).fetchone()[0])
            nodes = db.execute(
                """SELECT worker_id, health, last_seen_at FROM worker_nodes
                   WHERE target='gmail'""",
            ).fetchall()
        node_map: dict[str, list[tuple[str, str]]] = {}
        for worker_id, health, last_seen_at in nodes:
            worker_id = str(worker_id)
            for row in rows:
                base = str(row[1])
                if worker_id == base or worker_id.startswith(f"{base}-"):
                    node_map.setdefault(base, []).append((str(health), str(last_seen_at)))
                    break

        items: list[dict[str, Any]] = []
        for row in rows:
            account_id, worker_id = str(row[0]), str(row[1])
            cooldown_until = row[8]
            cooling = bool(cooldown_until and str(cooldown_until) > now.isoformat())
            health_rows = node_map.get(worker_id, [])
            health = "healthy" if any(
                value[0] == "healthy" and value[1] >= stale_before for value in health_rows
            ) else (
                "offline" if any(value[0] == "offline" for value in health_rows) else
                "stale" if any(value[0] == "stale" or value[1] < stale_before for value in health_rows)
                else "unknown"
            )
            last_seen_at = max((value[1] for value in health_rows), default=None)
            status = "disabled" if not bool(row[2]) else "cooldown" if cooling else (
                "active" if bool(row[3]) else "idle"
            )
            items.append({
                "account_id": account_id, "worker_id": worker_id,
                "enabled": bool(row[2]), "active": bool(row[3]), "status": status,
                "health": health, "last_seen_at": last_seen_at,
                "usage_date": row[4], "claimed_units": int(row[5]),
                "claimed_tasks": int(row[6]), "failure_count": int(row[7]),
                "cooldown_until": cooldown_until, "last_claimed_at": row[9],
                "soft_quota_units": int(row[10]),
            })
        return items

    def dashboard_snapshot(self) -> dict[str, Any]:
        """Return the account cards plus safe pool-level counters."""
        items = self.snapshot()
        now = _now().isoformat()
        return {
            "items": items,
            "summary": {
                "total_accounts": len(items),
                "active_accounts": sum(1 for item in items if item["status"] == "active"),
                "cooldown_accounts": sum(1 for item in items if item["status"] == "cooldown"),
                "today_units": sum(int(item["claimed_units"]) for item in items),
                "today_tasks": sum(int(item["claimed_tasks"]) for item in items),
                "queue_depth": self._queue_depth(),
                "updated_at": now,
            },
        }

    def _queue_depth(self) -> int:
        self.initialize()
        with closing(self._connect()) as db:
            return int(db.execute(
                """SELECT COUNT(*) FROM jobs
                   WHERE execution_target='gmail' AND status IN ('queued', 'running')
                     AND EXISTS (SELECT 1 FROM job_results r WHERE r.job_id=jobs.id
                                AND r.progress_state='pending')"""
            ).fetchone()[0])


cloudshell_coordinator = CloudShellCoordinator()
