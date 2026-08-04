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
                    PRIMARY KEY(worker_id, usage_date)
                )"""
            )
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
                        (worker_id, usage_date, account_id, enabled)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(worker_id, usage_date) DO UPDATE SET
                            account_id=excluded.account_id,
                            enabled=excluded.enabled""",
                    (worker_id, today, account_id, int(bool(item.get("enabled", True)))),
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

    def reserve(self, worker_id: str, reserved_units: int) -> str | None:
        """Reserve a claim slot if this worker is currently least-used."""
        if not self.enabled:
            return "disabled"
        # Refresh the manifest at claim time so adding an account only requires
        # editing the protected JSON file and restarting workers is optional.
        self.sync_accounts()
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
                   WHERE u.usage_date=? AND u.enabled=1
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
        with closing(self._connect()) as db:
            rows = db.execute(
                """SELECT account_id, worker_id, enabled, usage_date, claimed_units,
                          claimed_tasks, failure_count, cooldown_until, last_claimed_at
                   FROM cloudshell_account_usage WHERE usage_date=?
                   ORDER BY claimed_units, last_claimed_at, worker_id""",
                (_day(),),
            ).fetchall()
        return [
            {"account_id": r[0], "worker_id": r[1], "enabled": bool(r[2]), "usage_date": r[3],
             "claimed_units": int(r[4]), "claimed_tasks": int(r[5]), "failure_count": int(r[6]),
             "cooldown_until": r[7], "last_claimed_at": r[8]}
            for r in rows
        ]


cloudshell_coordinator = CloudShellCoordinator()
