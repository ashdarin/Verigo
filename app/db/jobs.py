from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import settings


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Job:
    id: str
    emails: list[str]
    worker_count: int
    status: str = "queued"
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    results: list[dict[str, Any]] = field(default_factory=list)
    csv_path: Path | None = None
    verifier: Any = None
    owner_id: str | None = None
    guest_token_hash: str | None = None
    guest_token: str | None = None
    worker_id: str | None = None
    heartbeat_at: datetime | None = None
    stop_on_deliverable: bool = False
    execution_target: str = "local"
    parent_id: str | None = None


@dataclass(frozen=True)
class WorkerRuntime:
    target: str
    worker_id: str | None = None
    last_seen_at: datetime | None = None
    wake_requested_at: datetime | None = None
    wake_deadline_at: datetime | None = None
    wake_attempts: int = 0
    last_wake_error: str | None = None
    idle_since: datetime | None = None
    stop_requested_at: datetime | None = None
    last_stop_error: str | None = None


class JobStore:
    """SQLite-backed queue, history store, result cache, and Catch-all archive."""

    _columns = (
        "id", "emails_json", "worker_count", "status", "created_at",
        "started_at", "finished_at", "error", "results_json", "csv_path",
        "owner_id", "guest_token_hash", "worker_id", "heartbeat_at", "stop_on_deliverable",
        "execution_target", "parent_id",
    )

    def __init__(self, keep: int = 100) -> None:
        self._keep = keep
        self._lock = threading.RLock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(settings.database_path, timeout=30, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @classmethod
    def _select_columns(cls) -> str:
        return ", ".join(cls._columns)

    @classmethod
    def _job_from_row(cls, raw_row: tuple[Any, ...]) -> Job:
        row = dict(zip(cls._columns, raw_row))
        return Job(
            id=row["id"],
            emails=json.loads(row["emails_json"]),
            worker_count=row["worker_count"],
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
            error=row["error"],
            results=json.loads(row["results_json"]),
            csv_path=Path(row["csv_path"]) if row["csv_path"] else None,
            owner_id=row["owner_id"],
            guest_token_hash=row["guest_token_hash"],
            worker_id=row["worker_id"],
            heartbeat_at=datetime.fromisoformat(row["heartbeat_at"]) if row["heartbeat_at"] else None,
            stop_on_deliverable=bool(row["stop_on_deliverable"]),
            execution_target=str(row["execution_target"] or "local"),
            parent_id=row["parent_id"],
        )

    def initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            settings.database_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                        id TEXT PRIMARY KEY,
                        emails_json TEXT NOT NULL,
                        worker_count INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        started_at TEXT,
                        finished_at TEXT,
                        error TEXT,
                        results_json TEXT NOT NULL DEFAULT '[]',
                        csv_path TEXT,
                        owner_id TEXT,
                        guest_token_hash TEXT,
                        worker_id TEXT,
                        heartbeat_at TEXT,
                        stop_on_deliverable INTEGER NOT NULL DEFAULT 0,
                        execution_target TEXT NOT NULL DEFAULT 'local',
                        parent_id TEXT
                    )
                    """
                )
                existing = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
                for name, kind in (("owner_id", "TEXT"), ("guest_token_hash", "TEXT"), ("worker_id", "TEXT"), ("heartbeat_at", "TEXT"), ("stop_on_deliverable", "INTEGER NOT NULL DEFAULT 0"), ("execution_target", "TEXT NOT NULL DEFAULT 'local'"), ("parent_id", "TEXT")):
                    if name not in existing:
                        connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {kind}")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status, created_at)")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_parent ON jobs(parent_id, created_at)")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS verification_cache (
                        email TEXT PRIMARY KEY,
                        result_json TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS verified_emails (
                        email TEXT PRIMARY KEY,
                        first_confirmed_at TEXT NOT NULL,
                        last_confirmed_at TEXT NOT NULL,
                        result_json TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS catch_all_emails (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL,
                        email TEXT NOT NULL,
                        domain TEXT NOT NULL,
                        verified_at TEXT NOT NULL,
                        result_json TEXT NOT NULL,
                        UNIQUE(job_id, email)
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_catch_all_domain ON catch_all_emails(domain, verified_at DESC)"
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS worker_runtime (
                        target TEXT PRIMARY KEY,
                        worker_id TEXT,
                        last_seen_at TEXT,
                        wake_requested_at TEXT,
                        wake_deadline_at TEXT,
                        wake_attempts INTEGER NOT NULL DEFAULT 0,
                        last_wake_error TEXT,
                        idle_since TEXT,
                        stop_requested_at TEXT,
                        last_stop_error TEXT
                    )
                    """
                )
            self._initialized = True

    def add(self, job: Job, max_active: int | None = None) -> None:
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')"
            ).fetchone()[0]
            if max_active is not None and active >= max_active:
                connection.rollback()
                raise RuntimeError("任务队列已满，请等待已有任务完成")
            connection.commit()
        self.persist(job)

    def persist(self, job: Job) -> None:
        self.initialize()
        values = (
            job.id,
            json.dumps(job.emails, ensure_ascii=False),
            job.worker_count,
            job.status,
            job.created_at.isoformat(),
            job.started_at.isoformat() if job.started_at else None,
            job.finished_at.isoformat() if job.finished_at else None,
            job.error,
            json.dumps(job.results, ensure_ascii=False, default=str),
            str(job.csv_path) if job.csv_path else None,
            job.owner_id,
            job.guest_token_hash,
            job.worker_id,
            job.heartbeat_at.isoformat() if job.heartbeat_at else None,
            int(job.stop_on_deliverable),
            job.execution_target,
            job.parent_id,
        )
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, emails_json, worker_count, status, created_at, started_at, finished_at,
                    error, results_json, csv_path, owner_id, guest_token_hash, worker_id, heartbeat_at,
                    stop_on_deliverable, execution_target, parent_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    emails_json=excluded.emails_json, worker_count=excluded.worker_count,
                    status=excluded.status, started_at=excluded.started_at,
                    finished_at=excluded.finished_at, error=excluded.error,
                    results_json=excluded.results_json, csv_path=excluded.csv_path,
                    owner_id=excluded.owner_id, guest_token_hash=excluded.guest_token_hash,
                    worker_id=excluded.worker_id, heartbeat_at=excluded.heartbeat_at,
                    stop_on_deliverable=excluded.stop_on_deliverable,
                    execution_target=excluded.execution_target, parent_id=excluded.parent_id
                WHERE jobs.status != 'stopped' OR excluded.status = 'stopped'
                """,
                values,
            )

    def get(self, job_id: str) -> Job | None:
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute(
                f"SELECT {self._select_columns()} FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._job_from_row(row) if row else None

    def list_recent(self, owner_id: str, limit: int = 20) -> list[Job]:
        self.initialize()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT {self._select_columns()} FROM jobs WHERE owner_id = ? AND parent_id IS NULL ORDER BY created_at DESC LIMIT ?",
                (owner_id, limit),
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def children(self, parent_id: str) -> list[Job]:
        self.initialize()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT {self._select_columns()} FROM jobs WHERE parent_id=? ORDER BY created_at, id",
                (parent_id,),
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def refresh_parent(self, parent_id: str) -> Job | None:
        """Merge child results into the user-visible parent task."""
        parent = self.get(parent_id)
        if parent is None or parent.status == "stopped":
            return parent
        children = self.children(parent_id)
        if not children:
            return parent

        results_by_email = {
            str(result.get("email", "")).lower(): dict(result)
            for child in children
            for result in child.results
            if result.get("email")
        }
        parent.results = []
        for index, email in enumerate(parent.emails):
            result = results_by_email.get(email.lower())
            if result is None:
                continue
            result["original_index"] = index
            parent.results.append(result)

        started = [child.started_at for child in children if child.started_at]
        parent.started_at = min(started) if started else None
        terminal = {"completed", "failed", "stopped"}
        if all(child.status in terminal for child in children):
            parent.finished_at = max(
                (child.finished_at or utc_now() for child in children), default=utc_now()
            )
            failures = [child.error for child in children if child.status == "failed" and child.error]
            if failures:
                parent.status = "failed"
                parent.error = "；".join(failures[:2])[:500]
            elif any(child.status == "stopped" for child in children):
                parent.status = "stopped"
                parent.error = "已由用户停止验证"
            else:
                parent.status = "completed"
                parent.error = None
        else:
            parent.status = "running"
            parent.finished_at = None
            notices = [child.error for child in children if child.status == "queued" and child.error]
            parent.error = notices[0] if notices else None
        self.persist(parent)
        return self.get(parent_id)

    def claim_next(self, worker_id: str, execution_target: str = "local") -> Job | None:
        """Atomically claim the next task; expired worker leases are returned to the queue."""
        self.initialize()
        now = utc_now()
        stale_before = now - timedelta(seconds=settings.worker_lease_seconds)
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE jobs SET status = 'queued', worker_id = NULL, heartbeat_at = NULL,
                    error = '工作节点已重新领取任务'
                WHERE status = 'running' AND heartbeat_at IS NOT NULL AND heartbeat_at < ?
                """,
                (stale_before.isoformat(),),
            )
            row = connection.execute(
                f"SELECT {self._select_columns()} FROM jobs WHERE status = 'queued' AND execution_target = ? ORDER BY created_at LIMIT 1",
                (execution_target,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            job = self._job_from_row(row)
            job.status = "running"
            job.worker_id = worker_id
            job.started_at = job.started_at or now
            job.heartbeat_at = now
            connection.execute(
                """
                UPDATE jobs SET status = 'running', worker_id = ?, started_at = ?, heartbeat_at = ?, error = NULL
                WHERE id = ?
                """,
                (worker_id, job.started_at.isoformat(), now.isoformat(), job.id),
            )
            connection.commit()
        return job

    def heartbeat(self, job: Job) -> None:
        job.heartbeat_at = utc_now()
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE jobs SET heartbeat_at = ? WHERE id = ? AND worker_id = ? AND status = 'running'",
                (job.heartbeat_at.isoformat(), job.id, job.worker_id),
            )

    def requeue_stale_jobs(self) -> int:
        """Return expired leases to their original execution-target queue."""
        self.initialize()
        stale_before = utc_now() - timedelta(seconds=settings.worker_lease_seconds)
        with closing(self._connect()) as connection:
            return connection.execute(
                """
                UPDATE jobs SET status='queued', worker_id=NULL, heartbeat_at=NULL,
                    error='工作节点已重新领取任务'
                WHERE status='running' AND heartbeat_at IS NOT NULL AND heartbeat_at < ?
                """,
                (stale_before.isoformat(),),
            ).rowcount

    def active_target_count(self, target: str) -> int:
        self.initialize()
        with closing(self._connect()) as connection:
            return int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM jobs
                    WHERE execution_target=? AND status IN ('queued', 'running')
                    """,
                    (target,),
                ).fetchone()[0]
            )

    def set_queued_target_message(self, target: str, message: str | None) -> int:
        self.initialize()
        with closing(self._connect()) as connection:
            return connection.execute(
                "UPDATE jobs SET error=? WHERE execution_target=? AND status='queued'",
                (message, target),
            ).rowcount

    def fail_queued_target(self, target: str, message: str) -> int:
        self.initialize()
        now = utc_now().isoformat()
        with closing(self._connect()) as connection:
            return connection.execute(
                """
                UPDATE jobs SET status='failed', error=?, finished_at=?,
                    worker_id=NULL, heartbeat_at=NULL
                WHERE execution_target=? AND status='queued'
                """,
                (message, now, target),
            ).rowcount

    @staticmethod
    def _runtime_from_row(target: str, row: tuple[Any, ...] | None) -> WorkerRuntime:
        if row is None:
            return WorkerRuntime(target=target)
        return WorkerRuntime(
            target=target,
            worker_id=row[0],
            last_seen_at=datetime.fromisoformat(row[1]) if row[1] else None,
            wake_requested_at=datetime.fromisoformat(row[2]) if row[2] else None,
            wake_deadline_at=datetime.fromisoformat(row[3]) if row[3] else None,
            wake_attempts=int(row[4]),
            last_wake_error=row[5],
            idle_since=datetime.fromisoformat(row[6]) if row[6] else None,
            stop_requested_at=datetime.fromisoformat(row[7]) if row[7] else None,
            last_stop_error=row[8],
        )

    def worker_runtime(self, target: str) -> WorkerRuntime:
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT worker_id, last_seen_at, wake_requested_at, wake_deadline_at,
                    wake_attempts, last_wake_error, idle_since, stop_requested_at,
                    last_stop_error
                FROM worker_runtime WHERE target=?
                """,
                (target,),
            ).fetchone()
        return self._runtime_from_row(target, row)

    def record_worker_seen(self, target: str, worker_id: str) -> None:
        self.initialize()
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO worker_runtime(target, worker_id, last_seen_at)
                VALUES (?, ?, ?)
                ON CONFLICT(target) DO UPDATE SET
                    worker_id=excluded.worker_id,
                    last_seen_at=excluded.last_seen_at,
                    wake_requested_at=NULL,
                    wake_deadline_at=NULL,
                    wake_attempts=0,
                    last_wake_error=NULL
                """,
                (target, worker_id, utc_now().isoformat()),
            )

    def record_wake_attempt(
        self, target: str, deadline: datetime | None, error: str | None
    ) -> WorkerRuntime:
        self.initialize()
        now = utc_now().isoformat()
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO worker_runtime(
                    target, wake_requested_at, wake_deadline_at, wake_attempts,
                    last_wake_error
                ) VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(target) DO UPDATE SET
                    wake_requested_at=excluded.wake_requested_at,
                    wake_deadline_at=excluded.wake_deadline_at,
                    wake_attempts=worker_runtime.wake_attempts+1,
                    last_wake_error=excluded.last_wake_error,
                    idle_since=NULL,
                    stop_requested_at=NULL,
                    last_stop_error=NULL
                """,
                (target, now, deadline.isoformat() if deadline else None, error),
            )
        return self.worker_runtime(target)

    def clear_wake_state(self, target: str) -> None:
        self.initialize()
        with closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE worker_runtime SET wake_requested_at=NULL, wake_deadline_at=NULL,
                    wake_attempts=0, last_wake_error=NULL
                WHERE target=?
                """,
                (target,),
            )

    def begin_worker_idle(self, target: str) -> WorkerRuntime:
        self.initialize()
        now = utc_now().isoformat()
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO worker_runtime(target, idle_since) VALUES (?, ?)
                ON CONFLICT(target) DO UPDATE SET
                    idle_since=COALESCE(worker_runtime.idle_since, excluded.idle_since)
                """,
                (target, now),
            )
        return self.worker_runtime(target)

    def clear_worker_idle(self, target: str) -> None:
        self.initialize()
        with closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE worker_runtime SET idle_since=NULL, stop_requested_at=NULL,
                    last_stop_error=NULL
                WHERE target=?
                """,
                (target,),
            )

    def record_stop_attempt(self, target: str, error: str | None) -> None:
        self.initialize()
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO worker_runtime(target, stop_requested_at, last_stop_error)
                VALUES (?, ?, ?)
                ON CONFLICT(target) DO UPDATE SET
                    stop_requested_at=excluded.stop_requested_at,
                    last_stop_error=excluded.last_stop_error
                """,
                (target, utc_now().isoformat(), error),
            )

    def is_stopped(self, job_id: str) -> bool:
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        return row is not None and row[0] == "stopped"

    def stop(self, job_id: str) -> Job | None:
        """Stop a queued or running job without discarding completed results."""
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT {self._select_columns()} FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            job = self._job_from_row(row)
            if job.status not in {"queued", "running"}:
                connection.commit()
                return job
            if job.execution_target == "aggregate":
                connection.execute(
                    """
                    UPDATE jobs SET status='stopped', finished_at=?, error=?,
                        worker_id=NULL, heartbeat_at=NULL
                    WHERE parent_id=? AND status IN ('queued', 'running')
                    """,
                    (utc_now().isoformat(), "已由用户停止验证", job_id),
                )
            connection.execute(
                """
                UPDATE jobs SET status='stopped', finished_at=?, error=?,
                    worker_id=NULL, heartbeat_at=NULL
                WHERE id=?
                """,
                (utc_now().isoformat(), "已由用户停止验证", job_id),
            )
            row = connection.execute(
                f"SELECT {self._select_columns()} FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            connection.commit()
        return self._job_from_row(row)

    def queue_position(self, job_id: str) -> int | None:
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT status, created_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None or row[0] != "queued":
                return None
            return connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = 'queued' AND created_at <= ?", (row[1],)
            ).fetchone()[0]

    def cached_results(self, emails: list[str]) -> dict[str, dict[str, Any]]:
        self.initialize()
        now = utc_now().isoformat()
        found: dict[str, dict[str, Any]] = {}
        with closing(self._connect()) as connection:
            for start in range(0, len(emails), 900):
                batch = [email.lower() for email in emails[start : start + 900]]
                placeholders = ", ".join("?" for _ in batch)
                rows = connection.execute(
                    f"SELECT email, result_json FROM verification_cache WHERE expires_at > ? AND email IN ({placeholders})",
                    (now, *batch),
                ).fetchall()
                for email, result_json in rows:
                    result = json.loads(result_json)
                    result["cache_hit"] = True
                    found[email] = result
            cutoff = (utc_now() - timedelta(days=settings.verified_email_recheck_days)).isoformat()
            unresolved = [email.lower() for email in emails if email.lower() not in found]
            for start in range(0, len(unresolved), 900):
                batch = unresolved[start : start + 900]
                if not batch:
                    continue
                placeholders = ", ".join("?" for _ in batch)
                rows = connection.execute(
                    f"SELECT email, result_json FROM verified_emails WHERE last_confirmed_at > ? AND email IN ({placeholders})",
                    (cutoff, *batch),
                ).fetchall()
                for email, result_json in rows:
                    result = json.loads(result_json)
                    result["cache_hit"] = True
                    result["verified_record"] = True
                    found[email] = result
        return found

    def cache_results(self, results: list[dict[str, Any]]) -> None:
        self.initialize()
        now = utc_now()
        rows: list[tuple[str, str, str, str]] = []
        verified_rows: list[tuple[str, str, str, str]] = []
        for result in results:
            checks = result.get("checks") or {}
            detail = str(result.get("smtp_result") or "")
            cacheable = result.get("deliverable") is True
            cacheable = cacheable or (
                result.get("deliverable") is False
                and ("RCPT TO" in detail or "邮箱不存在" in detail)
            )
            cacheable = cacheable or checks.get("domain") is False or checks.get("mx") is False
            if cacheable and result.get("email"):
                rows.append(
                    (
                        str(result["email"]).lower(),
                        json.dumps(result, ensure_ascii=False, default=str),
                        (now + timedelta(hours=settings.verification_cache_hours)).isoformat(),
                        now.isoformat(),
                    )
                )
            if result.get("deliverable") is True and result.get("email"):
                verified_rows.append(
                    (
                        str(result["email"]).lower(),
                        now.isoformat(),
                        now.isoformat(),
                        json.dumps(result, ensure_ascii=False, default=str),
                    )
                )
        if not rows and not verified_rows:
            return
        with closing(self._connect()) as connection:
            if rows:
                connection.executemany(
                    """
                    INSERT INTO verification_cache(email, result_json, expires_at, updated_at) VALUES (?, ?, ?, ?)
                    ON CONFLICT(email) DO UPDATE SET result_json=excluded.result_json,
                        expires_at=excluded.expires_at, updated_at=excluded.updated_at
                    """,
                    rows,
                )
            if verified_rows:
                connection.executemany(
                    """
                    INSERT INTO verified_emails(email, first_confirmed_at, last_confirmed_at, result_json)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(email) DO UPDATE SET last_confirmed_at=excluded.last_confirmed_at,
                        result_json=excluded.result_json
                    """,
                    verified_rows,
                )

    def record_catch_all(self, job: Job) -> None:
        rows = []
        for result in job.results:
            if result.get("domain_type") != "catch-all" or not result.get("email"):
                continue
            email = str(result["email"])
            rows.append(
                (
                    job.id,
                    email,
                    email.rsplit("@", 1)[-1].lower(),
                    str(result.get("timestamp") or utc_now().isoformat()),
                    json.dumps(result, ensure_ascii=False, default=str),
                )
            )
        if not rows:
            return
        self.initialize()
        with closing(self._connect()) as connection:
            connection.executemany(
                """
                INSERT INTO catch_all_emails(job_id, email, domain, verified_at, result_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(job_id, email) DO UPDATE SET result_json=excluded.result_json,
                    verified_at=excluded.verified_at
                """,
                rows,
            )


job_store = JobStore()
