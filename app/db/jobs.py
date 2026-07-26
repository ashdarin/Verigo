from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
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
    retry_parent_id: str | None = None
    deferred_retry_at: datetime | None = None
    temporary_retry_attempts: int = 0
    pending_indices: list[int] = field(default_factory=list)
    lease_id: str | None = None


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
        "execution_target", "parent_id", "retry_parent_id",
        "deferred_retry_at", "temporary_retry_attempts",
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
            retry_parent_id=row["retry_parent_id"],
            deferred_retry_at=(
                datetime.fromisoformat(row["deferred_retry_at"])
                if row["deferred_retry_at"] else None
            ),
            temporary_retry_attempts=int(row["temporary_retry_attempts"] or 0),
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
                        parent_id TEXT,
                        retry_parent_id TEXT,
                        deferred_retry_at TEXT,
                        temporary_retry_attempts INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                existing = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
                for name, kind in (("owner_id", "TEXT"), ("guest_token_hash", "TEXT"), ("worker_id", "TEXT"), ("heartbeat_at", "TEXT"), ("stop_on_deliverable", "INTEGER NOT NULL DEFAULT 0"), ("execution_target", "TEXT NOT NULL DEFAULT 'local'"), ("parent_id", "TEXT"), ("retry_parent_id", "TEXT"), ("deferred_retry_at", "TEXT"), ("temporary_retry_attempts", "INTEGER NOT NULL DEFAULT 0")):
                    if name not in existing:
                        connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {kind}")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status, created_at)")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_parent ON jobs(parent_id, created_at)")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_retry_parent ON jobs(retry_parent_id, created_at)")
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS job_results (
                        job_id TEXT NOT NULL, original_index INTEGER NOT NULL, email TEXT NOT NULL,
                        progress_state TEXT NOT NULL DEFAULT 'pending', result_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL, PRIMARY KEY (job_id, original_index)
                    )
                """)
                connection.execute("CREATE INDEX IF NOT EXISTS idx_job_results_pending ON job_results(job_id, progress_state, original_index)")
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS job_leases (
                        id TEXT PRIMARY KEY, job_id TEXT NOT NULL, worker_id TEXT NOT NULL,
                        execution_target TEXT NOT NULL, indices_json TEXT NOT NULL,
                        claimed_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, completed_at TEXT
                    )
                """)
                connection.execute("CREATE INDEX IF NOT EXISTS idx_job_leases_active ON job_leases(job_id, completed_at, heartbeat_at)")
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS worker_nodes (
                        target TEXT NOT NULL, worker_id TEXT NOT NULL, capacity INTEGER NOT NULL DEFAULT 1,
                        health TEXT NOT NULL DEFAULT 'healthy', last_seen_at TEXT NOT NULL,
                        PRIMARY KEY (target, worker_id)
                    )
                """)
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS mx_scheduler_leases (
                        lease_id TEXT NOT NULL, mx_key TEXT NOT NULL, expires_at TEXT NOT NULL,
                        PRIMARY KEY (lease_id, mx_key)
                    )
                """)
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS scheduler_owner_turns (
                        target TEXT NOT NULL, owner_key TEXT NOT NULL, last_claimed_at TEXT NOT NULL,
                        PRIMARY KEY (target, owner_key)
                    )
                """)
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
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS worker_heartbeats (
                        target TEXT NOT NULL,
                        worker_id TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        PRIMARY KEY (target, worker_id)
                    )
                    """
                )
                self._backfill_result_rows(connection)
            self._initialized = True

    @staticmethod
    def _result_state(result: dict[str, Any]) -> str:
        state = str(result.get("progress_state") or "").lower()
        if state in {"pending", "verifying", "completed", "failed", "stopped"}:
            return state
        return "completed"

    def _backfill_result_rows(self, connection: sqlite3.Connection) -> None:
        """Migrate legacy JSON snapshots once, without overwriting newer result rows."""
        if connection.execute("SELECT 1 FROM job_results LIMIT 1").fetchone() is not None:
            return
        now = utc_now().isoformat()
        for job_id, emails_json, results_json in connection.execute(
            "SELECT id, emails_json, results_json FROM jobs"
        ):
            try:
                emails, results = json.loads(emails_json), json.loads(results_json)
            except json.JSONDecodeError:
                continue
            indexed = {int(item.get("original_index", index)): item for index, item in enumerate(results) if isinstance(item, dict)}
            rows = []
            for index, email in enumerate(emails):
                result = dict(indexed.get(index, {"email": email, "original_index": index, "progress_state": "pending"}))
                result["email"], result["original_index"] = str(result.get("email") or email), index
                rows.append((job_id, index, result["email"], self._result_state(result), json.dumps(result, ensure_ascii=False, default=str), now))
            if rows:
                connection.executemany("""
                    INSERT INTO job_results(job_id, original_index, email, progress_state, result_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(job_id, original_index) DO NOTHING
                """, rows)

    def release_legacy_deferred_retries(self) -> int:
        """Release only tasks queued by the retired multi-minute retry policy."""
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            return connection.execute(
                """
                UPDATE jobs SET deferred_retry_at = NULL, temporary_retry_attempts = 0
                WHERE status = 'queued' AND deferred_retry_at IS NOT NULL
                    AND error LIKE '%自动复核%'
                """
            ).rowcount

    def clear_completed_retry_notices(self) -> int:
        """Remove obsolete retry notices after a repaired task has completed."""
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            return connection.execute(
                """
                UPDATE jobs SET error = NULL
                WHERE status = 'completed' AND error LIKE '检测到未完成的 SMTP 临时结果%'
                """
            ).rowcount

    def clear_dns_negative_cache(self) -> int:
        """Remove stale DNS-negative results after resolver logic changes."""
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT email, result_json FROM verification_cache"
            ).fetchall()
            stale = []
            for email, result_json in rows:
                try:
                    checks = json.loads(result_json).get("checks") or {}
                except json.JSONDecodeError:
                    continue
                if checks.get("domain") is False or checks.get("mx") is False:
                    stale.append((email,))
            if stale:
                connection.executemany(
                    "DELETE FROM verification_cache WHERE email=?", stale
                )
            return len(stale)

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
            "[]",
            str(job.csv_path) if job.csv_path else None,
            job.owner_id,
            job.guest_token_hash,
            job.worker_id,
            job.heartbeat_at.isoformat() if job.heartbeat_at else None,
            int(job.stop_on_deliverable),
            job.execution_target,
            job.parent_id,
            job.retry_parent_id,
            job.deferred_retry_at.isoformat() if job.deferred_retry_at else None,
            job.temporary_retry_attempts,
        )
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, emails_json, worker_count, status, created_at, started_at, finished_at,
                    error, results_json, csv_path, owner_id, guest_token_hash, worker_id, heartbeat_at,
                    stop_on_deliverable, execution_target, parent_id, retry_parent_id, deferred_retry_at,
                    temporary_retry_attempts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    emails_json=excluded.emails_json, worker_count=excluded.worker_count,
                    status=excluded.status, started_at=excluded.started_at,
                    finished_at=excluded.finished_at, error=excluded.error,
                    csv_path=excluded.csv_path,
                    owner_id=excluded.owner_id, guest_token_hash=excluded.guest_token_hash,
                    worker_id=excluded.worker_id, heartbeat_at=excluded.heartbeat_at,
                    stop_on_deliverable=excluded.stop_on_deliverable,
                    execution_target=excluded.execution_target, parent_id=excluded.parent_id,
                    retry_parent_id=excluded.retry_parent_id,
                    deferred_retry_at=excluded.deferred_retry_at,
                    temporary_retry_attempts=excluded.temporary_retry_attempts
                WHERE jobs.status != 'stopped' OR excluded.status = 'stopped'
                """,
                values,
            )
        if job.results:
            self.upsert_results(job.id, job.results)
        else:
            self.ensure_result_rows(job)

    def ensure_result_rows(self, job: Job) -> None:
        """Support legacy callers that created a job before visible waiting rows."""
        if not job.emails:
            return
        now = utc_now().isoformat()
        rows = [
            (job.id, index, email, "pending", json.dumps({
                "email": email, "original_index": index, "progress_state": "pending"
            }, ensure_ascii=False), now)
            for index, email in enumerate(job.emails)
        ]
        with self._lock, closing(self._connect()) as connection:
            connection.executemany("""
                INSERT INTO job_results(job_id, original_index, email, progress_state, result_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(job_id, original_index) DO NOTHING
            """, rows)

    def results_for_job(self, job_id: str) -> list[dict[str, Any]]:
        self.initialize()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT progress_state, result_json FROM job_results WHERE job_id=? ORDER BY original_index", (job_id,)
            ).fetchall()
        results = []
        for state, raw in rows:
            result = json.loads(raw)
            result["progress_state"] = state
            results.append(result)
        return results

    def upsert_results(self, job_id: str, results: list[dict[str, Any]]) -> None:
        """Write result deltas in one transaction; terminal rows cannot regress."""
        if not results:
            return
        self.initialize()
        now = utc_now().isoformat()
        rows = []
        for fallback_index, raw in enumerate(results):
            result = dict(raw)
            index = int(result.get("original_index", fallback_index))
            result["original_index"] = index
            email = str(result.get("email") or "")
            rows.append((job_id, index, email, self._result_state(result), json.dumps(result, ensure_ascii=False, default=str), now))
        with self._lock, closing(self._connect()) as connection:
            connection.executemany("""
                INSERT INTO job_results(job_id, original_index, email, progress_state, result_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id, original_index) DO UPDATE SET
                    email=excluded.email, progress_state=excluded.progress_state,
                    result_json=excluded.result_json, updated_at=excluded.updated_at
                WHERE job_results.progress_state IN ('pending', 'verifying')
                    OR excluded.progress_state NOT IN ('pending', 'verifying')
            """, rows)

    def _hydrate_results(self, job: Job) -> Job:
        job.results = self.results_for_job(job.id)
        return job

    def get(self, job_id: str, include_results: bool = True) -> Job | None:
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute(
                f"SELECT {self._select_columns()} FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if not row:
            return None
        job = self._job_from_row(row)
        return self._hydrate_results(job) if include_results else job

    def list_recent(self, owner_id: str, limit: int = 20) -> list[Job]:
        self.initialize()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT {self._select_columns()} FROM jobs WHERE owner_id = ? AND parent_id IS NULL AND retry_parent_id IS NULL ORDER BY created_at DESC LIMIT ?",
                (owner_id, limit),
            ).fetchall()
        return [self._hydrate_results(self._job_from_row(row)) for row in rows]

    def recent_completed_single_jobs(self, since: datetime) -> list[Job]:
        """Return standalone single-address jobs eligible for a narrow repair pass."""
        self.initialize()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""SELECT {self._select_columns()} FROM jobs
                WHERE status='completed' AND parent_id IS NULL AND execution_target != 'aggregate'
                    AND retry_parent_id IS NULL AND created_at >= ? AND emails_json NOT LIKE '%,%'
                ORDER BY created_at""",
                (since.isoformat(),),
            ).fetchall()
        return [self._hydrate_results(self._job_from_row(row)) for row in rows]

    def children(self, parent_id: str) -> list[Job]:
        self.initialize()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT {self._select_columns()} FROM jobs WHERE parent_id=? ORDER BY created_at, id",
                (parent_id,),
            ).fetchall()
        return [self._hydrate_results(self._job_from_row(row)) for row in rows]

    def retry_children(self, parent_id: str) -> list[Job]:
        self.initialize()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT {self._select_columns()} FROM jobs WHERE retry_parent_id=? ORDER BY created_at, id",
                (parent_id,),
            ).fetchall()
        return [self._hydrate_results(self._job_from_row(row)) for row in rows]

    def has_active_retry_child(self, parent_id: str) -> bool:
        """Whether a deferred recheck is already queued or running for this task."""
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT 1 FROM jobs
                WHERE retry_parent_id=? AND status IN ('queued', 'running') LIMIT 1""",
                (parent_id,),
            ).fetchone()
        return row is not None

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
                f"""SELECT {self._select_columns()} FROM jobs
                WHERE status = 'queued' AND execution_target = ?
                    AND (deferred_retry_at IS NULL OR deferred_retry_at <= ?)
                ORDER BY created_at LIMIT 1""",
                (execution_target, now.isoformat()),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            job = self._job_from_row(row)
            job.status = "running"
            job.worker_id = worker_id
            job.started_at = job.started_at or now
            job.heartbeat_at = now
            job.deferred_retry_at = None
            job.error = None
            for result in job.results:
                if result.get("progress_state") != "pending":
                    continue
                result["progress_state"] = "verifying"
                result["verification_method"] = "正在验证"
                result["smtp_result"] = "正在验证"
                result["message"] = "正在验证"
            connection.execute("""
                UPDATE job_results SET progress_state='verifying'
                WHERE job_id=? AND progress_state='pending'
            """, (job.id,))
            connection.execute(
                """
                UPDATE jobs SET status = 'running', worker_id = ?, started_at = ?, heartbeat_at = ?,
                    deferred_retry_at = NULL, error = NULL, results_json = ?
                WHERE id = ?
                """,
                (
                    worker_id, job.started_at.isoformat(), now.isoformat(),
                    json.dumps(job.results, ensure_ascii=False), job.id,
                ),
            )
            connection.commit()
        return job

    def claim_remote_lease(
        self, worker_id: str, execution_target: str, *, capacity: int = 1, shard_size: int = 100,
    ) -> Job | None:
        """Allocate only unfinished result indexes, allowing idle nodes to steal work."""
        self.initialize()
        now = utc_now()
        stale = now - timedelta(seconds=settings.worker_lease_seconds)
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM mx_scheduler_leases WHERE expires_at < ?", (now.isoformat(),))
            connection.execute("""
                INSERT INTO worker_nodes(target, worker_id, capacity, health, last_seen_at)
                VALUES (?, ?, ?, 'healthy', ?)
                ON CONFLICT(target, worker_id) DO UPDATE SET capacity=excluded.capacity,
                    health='healthy', last_seen_at=excluded.last_seen_at
            """, (execution_target, worker_id, max(1, capacity), now.isoformat()))
            load = connection.execute("""
                SELECT COUNT(*) FROM job_leases WHERE worker_id=? AND execution_target=?
                    AND completed_at IS NULL AND heartbeat_at >= ?
            """, (worker_id, execution_target, stale.isoformat())).fetchone()[0]
            if load >= max(1, capacity):
                connection.commit()
                return None
            row = connection.execute(f"""
                SELECT {', '.join('j.' + column for column in self._columns)} FROM jobs j
                LEFT JOIN jobs parent ON parent.id=j.parent_id
                LEFT JOIN scheduler_owner_turns turn ON turn.target=?
                    AND turn.owner_key=COALESCE(parent.owner_id, j.owner_id, j.id)
                WHERE j.status IN ('queued', 'running') AND j.execution_target=?
                    AND (j.deferred_retry_at IS NULL OR j.deferred_retry_at <= ?)
                    AND EXISTS (SELECT 1 FROM job_results r WHERE r.job_id=j.id
                        AND r.progress_state IN ('pending', 'verifying'))
                ORDER BY COALESCE(turn.last_claimed_at, '1970-01-01T00:00:00+00:00'), j.created_at LIMIT 1
            """, (execution_target, execution_target, now.isoformat())).fetchone()
            if row is None:
                connection.commit()
                return None
            job = self._job_from_row(row)
            owner_key = connection.execute("""
                SELECT COALESCE(parent.owner_id, child.owner_id, child.id) FROM jobs child
                LEFT JOIN jobs parent ON parent.id=child.parent_id WHERE child.id=?
            """, (job.id,)).fetchone()[0]
            connection.execute("""
                INSERT INTO scheduler_owner_turns(target, owner_key, last_claimed_at) VALUES (?, ?, ?)
                ON CONFLICT(target, owner_key) DO UPDATE SET last_claimed_at=excluded.last_claimed_at
            """, (execution_target, owner_key, now.isoformat()))
            leased = {index for (raw,) in connection.execute("""
                SELECT indices_json FROM job_leases WHERE job_id=? AND completed_at IS NULL
                    AND heartbeat_at >= ?
            """, (job.id, stale.isoformat())) for index in json.loads(raw)}
            throttled = {key for (key,) in connection.execute(
                "SELECT mx_key FROM mx_scheduler_leases WHERE expires_at >= ?", (now.isoformat(),)
            )}
            indices, mx_keys = [], set()
            for index, email in connection.execute("""
                SELECT original_index, email FROM job_results WHERE job_id=?
                    AND progress_state IN ('pending', 'verifying') ORDER BY original_index
            """, (job.id,)):
                mx_key = str(email).rsplit("@", 1)[-1].lower()
                if index in leased or mx_key in throttled or mx_key in mx_keys:
                    continue
                indices.append(int(index)); mx_keys.add(mx_key)
                if len(indices) >= max(1, shard_size):
                    break
            if not indices:
                connection.commit()
                return None
            lease_id = uuid.uuid4().hex
            connection.execute("""
                INSERT INTO job_leases(id, job_id, worker_id, execution_target, indices_json, claimed_at, heartbeat_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (lease_id, job.id, worker_id, execution_target, json.dumps(indices), now.isoformat(), now.isoformat()))
            expires = (now + timedelta(seconds=settings.worker_lease_seconds)).isoformat()
            connection.executemany("INSERT INTO mx_scheduler_leases(lease_id, mx_key, expires_at) VALUES (?, ?, ?)", [(lease_id, key, expires) for key in mx_keys])
            connection.execute("""
                UPDATE jobs SET status='running', worker_id=?, started_at=COALESCE(started_at, ?),
                    heartbeat_at=?, deferred_retry_at=NULL, error=NULL WHERE id=?
            """, (worker_id, now.isoformat(), now.isoformat(), job.id))
            connection.commit()
        job.status, job.worker_id, job.heartbeat_at = "running", worker_id, now
        job.started_at = job.started_at or now
        job.pending_indices, job.lease_id = indices, lease_id
        return job

    def lease_valid(self, job_id: str, worker_id: str, lease_id: str) -> bool:
        stale = (utc_now() - timedelta(seconds=settings.worker_lease_seconds)).isoformat()
        with closing(self._connect()) as connection:
            return connection.execute("""
                SELECT 1 FROM job_leases WHERE id=? AND job_id=? AND worker_id=?
                    AND completed_at IS NULL AND heartbeat_at >= ?
            """, (lease_id, job_id, worker_id, stale)).fetchone() is not None

    def heartbeat_lease(self, job_id: str, worker_id: str, lease_id: str) -> bool:
        now = utc_now()
        with closing(self._connect()) as connection:
            changed = connection.execute("""
                UPDATE job_leases SET heartbeat_at=? WHERE id=? AND job_id=? AND worker_id=?
                    AND completed_at IS NULL
            """, (now.isoformat(), lease_id, job_id, worker_id)).rowcount
            if changed:
                connection.execute("UPDATE mx_scheduler_leases SET expires_at=? WHERE lease_id=?", ((now + timedelta(seconds=settings.worker_lease_seconds)).isoformat(), lease_id))
                connection.execute("UPDATE jobs SET heartbeat_at=? WHERE id=?", (now.isoformat(), job_id))
        return bool(changed)

    def complete_lease(self, job_id: str, worker_id: str, lease_id: str) -> bool:
        with closing(self._connect()) as connection:
            changed = connection.execute("""
                UPDATE job_leases SET completed_at=? WHERE id=? AND job_id=? AND worker_id=?
                    AND completed_at IS NULL
            """, (utc_now().isoformat(), lease_id, job_id, worker_id)).rowcount
            connection.execute("DELETE FROM mx_scheduler_leases WHERE lease_id=?", (lease_id,))
        return bool(changed)

    def pending_count(self, job_id: str) -> int:
        with closing(self._connect()) as connection:
            return int(connection.execute("""
                SELECT COUNT(*) FROM job_results WHERE job_id=? AND progress_state IN ('pending', 'verifying')
            """, (job_id,)).fetchone()[0])

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
            queued = connection.execute(
                """SELECT id, parent_id, emails_json, results_json FROM jobs
                WHERE execution_target=? AND status='queued'""",
                (target,),
            ).fetchall()
            parents = [row[1] for row in queued if row[1] is not None]
            for job_id, _parent_id, emails_json, results_json in queued:
                emails = json.loads(emails_json)
                results = json.loads(results_json)
                by_email = {
                    str(result.get("email", "")).lower(): result
                    for result in results
                    if result.get("email")
                }
                for index, email in enumerate(emails):
                    result = by_email.get(str(email).lower())
                    if result is None:
                        result = {"email": email, "original_index": index}
                        results.append(result)
                    if result.get("progress_state") not in {None, "pending", "verifying"}:
                        continue
                    result.update({
                        "progress_state": "failed",
                        "verification_method": "验证未完成",
                        "smtp_result": "验证节点未能启动，尚未完成验证",
                        "message": message,
                        "deliverable": None,
                        "valid": None,
                    })
                connection.execute(
                    "UPDATE jobs SET results_json=? WHERE id=?",
                    (json.dumps(results, ensure_ascii=False), job_id),
                )
            connection.execute("""
                UPDATE job_results SET progress_state='failed'
                WHERE job_id IN (SELECT id FROM jobs WHERE execution_target=? AND status='queued')
                    AND progress_state IN ('pending', 'verifying')
            """, (target,))
            failed = connection.execute(
                """
                UPDATE jobs SET status='failed', error=?, finished_at=?,
                    worker_id=NULL, heartbeat_at=NULL
                WHERE execution_target=? AND status='queued'
                """,
                (message, now, target),
            ).rowcount
        for parent_id in parents:
            self.refresh_parent(str(parent_id))
        return failed

    def mark_unfinished_results_failed(self, job: Job, message: str) -> Job:
        """Expose every address affected when a worker fails before returning a result."""
        by_email = {
            str(result.get("email", "")).lower(): result
            for result in job.results
            if result.get("email")
        }
        for index, email in enumerate(job.emails):
            result = by_email.get(email.lower())
            if result is None:
                result = {"email": email, "original_index": index}
                job.results.append(result)
            elif result.get("progress_state") not in {"pending", "verifying"}:
                continue
            result.update({
                "progress_state": "failed",
                "verification_method": "验证未完成",
                "smtp_result": "验证节点未能启动，尚未完成验证",
                "message": message,
                "deliverable": None,
                "valid": None,
            })
        self.persist(job)
        return job

    def reconcile_failed_job_results(self) -> int:
        """Backfill address-level failure states for tasks created before live progress."""
        self.initialize()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""SELECT {self._select_columns()} FROM jobs
                WHERE status='failed' AND execution_target != 'aggregate'"""
            ).fetchall()
        parents: set[str] = set()
        repaired = 0
        for row in rows:
            job = self._job_from_row(row)
            before = json.dumps(job.results, sort_keys=True, ensure_ascii=False)
            self.mark_unfinished_results_failed(
                job, job.error or "验证任务未完成，请稍后重新提交"
            )
            after = json.dumps(job.results, sort_keys=True, ensure_ascii=False)
            if before == after:
                continue
            repaired += 1
            if job.parent_id:
                parents.add(job.parent_id)
        for parent_id in parents:
            self.refresh_parent(parent_id)
        return repaired

    def reconcile_aggregate_parents(self) -> int:
        """Repair visible parent states after worker-side failures or restarts."""
        self.initialize()
        with closing(self._connect()) as connection:
            parent_ids = [
                str(row[0]) for row in connection.execute(
                    "SELECT id FROM jobs WHERE execution_target='aggregate' AND status IN ('queued', 'running')"
                ).fetchall()
            ]
        for parent_id in parent_ids:
            self.refresh_parent(parent_id)
        return len(parent_ids)

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
        now = utc_now().isoformat()
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO worker_heartbeats(target, worker_id, last_seen_at)
                VALUES (?, ?, ?)
                ON CONFLICT(target, worker_id) DO UPDATE SET
                    last_seen_at=excluded.last_seen_at
                """,
                (target, worker_id, now),
            )
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
                (target, worker_id, now),
            )

    def worker_heartbeats(self, target: str) -> dict[str, datetime]:
        """Return the last heartbeat for every registered worker on a target."""
        self.initialize()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT worker_id, last_seen_at FROM worker_heartbeats
                WHERE target=? ORDER BY worker_id
                """,
                (target,),
            ).fetchall()
        return {
            str(worker_id): datetime.fromisoformat(last_seen_at)
            for worker_id, last_seen_at in rows
        }

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
            lease_ids = [item[0] for item in connection.execute(
                "SELECT id FROM job_leases WHERE job_id=? AND completed_at IS NULL", (job_id,)
            )]
            connection.execute(
                "UPDATE job_leases SET completed_at=? WHERE job_id=? AND completed_at IS NULL",
                (utc_now().isoformat(), job_id),
            )
            if lease_ids:
                connection.executemany(
                    "DELETE FROM mx_scheduler_leases WHERE lease_id=?", [(lease_id,) for lease_id in lease_ids]
                )
            row = connection.execute(
                f"SELECT {self._select_columns()} FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            connection.commit()
        return self._hydrate_results(self._job_from_row(row))

    def resume(self, job_id: str) -> tuple[Job | None, list[Job]]:
        """Resume a stopped task in place and return work that was re-queued."""
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT {self._select_columns()} FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                return None, []
            job = self._job_from_row(row)
            if job.status != "stopped":
                connection.commit()
                return job, []

            if job.execution_target == "aggregate":
                child_rows = connection.execute(
                    f"SELECT {self._select_columns()} FROM jobs "
                    "WHERE parent_id=? AND status='stopped' ORDER BY created_at, id",
                    (job_id,),
                ).fetchall()
                resumed_children = [self._job_from_row(child_row) for child_row in child_rows]
                if not resumed_children:
                    connection.commit()
                    return job, []
                connection.execute(
                    """
                    UPDATE jobs SET status='queued', finished_at=NULL, error=NULL,
                        worker_id=NULL, heartbeat_at=NULL, deferred_retry_at=NULL
                    WHERE parent_id=? AND status='stopped'
                    """,
                    (job_id,),
                )
                status = "running"
            else:
                resumed_children = [job]
                status = "queued"

            connection.execute(
                """
                UPDATE jobs SET status=?, finished_at=NULL, error=NULL,
                    worker_id=NULL, heartbeat_at=NULL, deferred_retry_at=NULL
                WHERE id=?
                """,
                (status, job_id),
            )
            connection.commit()
        resumed = self.get(job_id)
        return resumed, resumed_children

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
