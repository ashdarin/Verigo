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
from app.core.result_retry import is_recipient_mailbox_full, smtp_temporary_status
from app.core.verification_cache_policy import (
    cache_decision, is_cache_excluded, sanitize_cached_result,
)
from app.db.pg_compat import PgConnection, as_bool, as_datetime, as_json, postgres_active
from app.db.sqlite import begin_immediate, connect as connect_sqlite


_CACHE_METRIC_NAMES = (
    "lookups", "fresh_hits", "misses", "stale_seen",
    "writes_deliverable", "writes_permanent_invalid", "writes_mailbox_full",
    "coalesced_waiters", "refresh_scheduled",
)
_CACHE_METRICS_LOCK = threading.Lock()
_CACHE_METRICS_PENDING: dict[str, dict[str, int]] = {}
_CACHE_METRICS_FLUSHING = False


def _dt(value: Any) -> datetime | None:
    """Parse SQLite ISO strings or PostgreSQL datetime values."""
    return as_datetime(value)


def _sql_ts(value: Any) -> Any:
    """Bind timestamps as datetime on PG, ISO text on SQLite.

    Never compare a PostgreSQL ``timestamptz`` column to ``datetime.isoformat()``
    text. psycopg sends Python ``str`` as ``text``, so PostgreSQL may evaluate
    ``timestamptz < text`` by casting the column to text (``YYYY-MM-DD HH:MM``).
    ``isoformat()`` uses ``T`` in that position, and space < ``T``, so a fresh
    heartbeat looks older than any same-day cutoff and nodes flip offline.
    """
    if value is None or value == "":
        return None
    parsed = value if isinstance(value, datetime) else as_datetime(value)
    if parsed is None:
        return value
    return parsed if postgres_active() else parsed.isoformat()


def _json_load(value: Any, default: Any = None) -> Any:
    """Load JSON from SQLite text or accept PostgreSQL jsonb natives."""
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


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
    retry_route: str = "same_target"
    origin_execution_target: str | None = None
    cross_route_attempts: int = 0
    pending_indices: list[int] = field(default_factory=list)
    lease_id: str | None = None
    list_name: str | None = None
    is_cache_refresh: bool = False


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


@dataclass(frozen=True)
class ResultOverview:
    settled: int
    total: int
    valid: int
    deliverable: int
    undeliverable: int
    unknown: int
    catch_all: int
    retry_at: datetime | None
    review_updated: bool


@dataclass(frozen=True)
class WorkspaceOverview:
    total: int
    processed_today: int
    deliverable: int
    settled: int


class JobStore:
    """SQLite-backed queue, history store, result cache, and Catch-all archive."""

    _columns = (
        "id", "emails_json", "worker_count", "status", "created_at",
        "started_at", "finished_at", "error", "results_json", "csv_path",
        "owner_id", "guest_token_hash", "worker_id", "heartbeat_at", "stop_on_deliverable",
        "execution_target", "parent_id", "retry_parent_id",
        "deferred_retry_at", "temporary_retry_attempts", "retry_route",
        "origin_execution_target", "cross_route_attempts", "list_name", "is_cache_refresh",
    )

    def __init__(self, keep: int = 100) -> None:
        self._keep = keep
        self._lock = threading.RLock()
        self._initialized = False

    def _connect(self):
        from app.db.pg_compat import connect_app

        return connect_app()

    @classmethod
    def _select_columns(cls) -> str:
        return ", ".join(cls._columns)

    @classmethod
    def _job_from_row(cls, raw_row: tuple[Any, ...]) -> Job:
        row = dict(zip(cls._columns, raw_row))
        return Job(
            id=row["id"],
            emails=list(as_json(row["emails_json"], default=[]) or []),
            worker_count=row["worker_count"],
            status=row["status"],
            created_at=as_datetime(row["created_at"]) or utc_now(),
            started_at=as_datetime(row["started_at"]),
            finished_at=as_datetime(row["finished_at"]),
            error=row["error"],
            results=list(as_json(row["results_json"], default=[]) or []),
            csv_path=Path(row["csv_path"]) if row["csv_path"] else None,
            owner_id=row["owner_id"],
            guest_token_hash=row["guest_token_hash"],
            worker_id=row["worker_id"],
            heartbeat_at=as_datetime(row["heartbeat_at"]),
            stop_on_deliverable=as_bool(row["stop_on_deliverable"]),
            execution_target=str(row["execution_target"] or "local"),
            parent_id=row["parent_id"],
            retry_parent_id=row["retry_parent_id"],
            deferred_retry_at=as_datetime(row["deferred_retry_at"]),
            temporary_retry_attempts=int(row["temporary_retry_attempts"] or 0),
            retry_route=str(row["retry_route"] or "same_target"),
            origin_execution_target=row["origin_execution_target"],
            cross_route_attempts=int(row["cross_route_attempts"] or 0),
            list_name=row["list_name"],
            is_cache_refresh=as_bool(row["is_cache_refresh"]),
        )

    def initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            if postgres_active():
                self._initialized = True
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
                        temporary_retry_attempts INTEGER NOT NULL DEFAULT 0,
                        retry_route TEXT NOT NULL DEFAULT 'same_target',
                        origin_execution_target TEXT,
                        cross_route_attempts INTEGER NOT NULL DEFAULT 0,
                        list_name TEXT,
                        is_cache_refresh INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                existing = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
                for name, kind in (("owner_id", "TEXT"), ("guest_token_hash", "TEXT"), ("worker_id", "TEXT"), ("heartbeat_at", "TEXT"), ("stop_on_deliverable", "INTEGER NOT NULL DEFAULT 0"), ("execution_target", "TEXT NOT NULL DEFAULT 'local'"), ("parent_id", "TEXT"), ("retry_parent_id", "TEXT"), ("deferred_retry_at", "TEXT"), ("temporary_retry_attempts", "INTEGER NOT NULL DEFAULT 0"), ("retry_route", "TEXT NOT NULL DEFAULT 'same_target'"), ("origin_execution_target", "TEXT"), ("cross_route_attempts", "INTEGER NOT NULL DEFAULT 0"), ("list_name", "TEXT"), ("is_cache_refresh", "INTEGER NOT NULL DEFAULT 0")):
                    if name not in existing:
                        connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {kind}")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status, created_at)")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_parent ON jobs(parent_id, created_at)")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_retry_parent ON jobs(retry_parent_id, created_at)")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_cross_route_queue ON jobs(execution_target, retry_route, status, created_at)")
                connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS service_state (
                    name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"""
                )
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS job_results (
                        job_id TEXT NOT NULL, original_index INTEGER NOT NULL, email TEXT NOT NULL,
                        progress_state TEXT NOT NULL DEFAULT 'pending', result_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL, initial_completed_at TEXT, deliverability INTEGER,
                        is_valid INTEGER NOT NULL DEFAULT 0,
                        is_skipped INTEGER NOT NULL DEFAULT 0, is_catch_all INTEGER NOT NULL DEFAULT 0,
                        retry_at TEXT, retry_updated INTEGER NOT NULL DEFAULT 0,
                        query_fields_ready INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (job_id, original_index)
                    )
                """)
                result_columns = {row[1] for row in connection.execute("PRAGMA table_info(job_results)")}
                for name, kind in (
                    ("initial_completed_at", "TEXT"),
                    ("deliverability", "INTEGER"),
                    ("is_valid", "INTEGER NOT NULL DEFAULT 0"),
                    ("is_skipped", "INTEGER NOT NULL DEFAULT 0"),
                    ("is_catch_all", "INTEGER NOT NULL DEFAULT 0"),
                    ("retry_at", "TEXT"),
                    ("retry_updated", "INTEGER NOT NULL DEFAULT 0"),
                    ("query_fields_ready", "INTEGER NOT NULL DEFAULT 0"),
                ):
                    if name not in result_columns:
                        connection.execute(f"ALTER TABLE job_results ADD COLUMN {name} {kind}")
                connection.execute(
                    """UPDATE job_results
                    SET initial_completed_at=COALESCE(
                        (SELECT CASE
                            WHEN jobs.finished_at IS NOT NULL
                                AND jobs.finished_at < job_results.updated_at
                            THEN jobs.finished_at
                            ELSE job_results.updated_at
                        END FROM jobs WHERE jobs.id=job_results.job_id),
                        job_results.updated_at
                    )
                    WHERE initial_completed_at IS NULL
                        AND progress_state IN ('completed', 'failed', 'stopped')"""
                )
                connection.execute("CREATE INDEX IF NOT EXISTS idx_job_results_pending ON job_results(job_id, progress_state, original_index)")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_job_results_filter ON job_results(job_id, deliverability, is_skipped, original_index)")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_job_results_email ON job_results(job_id, email COLLATE NOCASE, original_index)")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_job_results_updated ON job_results(updated_at)")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_job_results_quality_window ON job_results(updated_at, progress_state)")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_job_results_initial_quality_window ON job_results(initial_completed_at, progress_state)")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_job_results_review_backlog ON job_results(retry_at)")
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS result_objects (
                        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, task_id TEXT NOT NULL,
                        result_index INTEGER NOT NULL, email TEXT NOT NULL,
                        status TEXT NOT NULL, verification_method TEXT,
                        server_response TEXT, confidence TEXT NOT NULL DEFAULT 'unknown',
                        source TEXT NOT NULL, created_at TEXT NOT NULL,
                        supersedes_result_id TEXT, metadata_json TEXT NOT NULL DEFAULT '{}',
                        UNIQUE(owner_id, task_id, result_index)
                    )
                """)
                connection.execute("CREATE INDEX IF NOT EXISTS idx_result_objects_owner ON result_objects(owner_id, created_at DESC)")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_result_objects_email ON result_objects(owner_id, email COLLATE NOCASE)")
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS lists (
                        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, name TEXT NOT NULL,
                        description TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL, archived_at TEXT
                    )
                """)
                connection.execute("CREATE INDEX IF NOT EXISTS idx_lists_owner ON lists(owner_id, archived_at, updated_at DESC)")
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS list_items (
                        list_id TEXT NOT NULL, result_id TEXT NOT NULL,
                        added_at TEXT NOT NULL, added_from TEXT NOT NULL,
                        PRIMARY KEY(list_id, result_id),
                        FOREIGN KEY(list_id) REFERENCES lists(id) ON DELETE CASCADE,
                        FOREIGN KEY(result_id) REFERENCES result_objects(id) ON DELETE CASCADE
                    )
                """)
                connection.execute("CREATE INDEX IF NOT EXISTS idx_list_items_result ON list_items(result_id)")
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS job_result_links (
                        child_job_id TEXT NOT NULL, child_index INTEGER NOT NULL,
                        parent_job_id TEXT NOT NULL, parent_index INTEGER NOT NULL,
                        PRIMARY KEY(child_job_id, child_index),
                        UNIQUE(parent_job_id, parent_index)
                    )
                """)
                connection.execute("CREATE INDEX IF NOT EXISTS idx_job_result_links_parent ON job_result_links(parent_job_id, parent_index)")
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
                        lease_id TEXT NOT NULL, mx_key TEXT NOT NULL, slots INTEGER NOT NULL DEFAULT 1,
                        expires_at TEXT NOT NULL,
                        PRIMARY KEY (lease_id, mx_key)
                    )
                """)
                mx_lease_columns = {row[1] for row in connection.execute("PRAGMA table_info(mx_scheduler_leases)")}
                if "slots" not in mx_lease_columns:
                    connection.execute("ALTER TABLE mx_scheduler_leases ADD COLUMN slots INTEGER NOT NULL DEFAULT 1")
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS scheduler_owner_turns (
                        target TEXT NOT NULL, owner_key TEXT NOT NULL, last_claimed_at TEXT NOT NULL,
                        PRIMARY KEY (target, owner_key)
                    )
                """)
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS scheduler_domain_profiles (
                        scheduler_key TEXT PRIMARY KEY,
                        current_limit INTEGER NOT NULL,
                        success_streak INTEGER NOT NULL DEFAULT 0,
                        successes INTEGER NOT NULL DEFAULT 0,
                        pressure_events INTEGER NOT NULL DEFAULT 0,
                        last_seen_at TEXT NOT NULL,
                        last_adjusted_at TEXT,
                        cooldown_until TEXT
                    )
                """)
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS scheduler_domain_routes (
                        domain TEXT PRIMARY KEY,
                        scheduler_key TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                """)
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS verification_cache (
                        email TEXT PRIMARY KEY,
                        result_json TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        outcome_class TEXT NOT NULL DEFAULT 'legacy',
                        verified_at TEXT,
                        stale_expires_at TEXT,
                        hit_count INTEGER NOT NULL DEFAULT 0,
                        last_hit_at TEXT,
                        refresh_requested_at TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS verified_emails (
                        email TEXT PRIMARY KEY,
                        first_confirmed_at TEXT NOT NULL,
                        last_confirmed_at TEXT NOT NULL,
                        result_json TEXT NOT NULL,
                        confirmation_count INTEGER NOT NULL DEFAULT 1
                    )
                    """
                )
                cache_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(verification_cache)")
                }
                for name, kind in (
                    ("outcome_class", "TEXT NOT NULL DEFAULT 'legacy'"),
                    ("verified_at", "TEXT"), ("stale_expires_at", "TEXT"),
                    ("hit_count", "INTEGER NOT NULL DEFAULT 0"), ("last_hit_at", "TEXT"),
                    ("refresh_requested_at", "TEXT"),
                ):
                    if name not in cache_columns:
                        connection.execute(
                            f"ALTER TABLE verification_cache ADD COLUMN {name} {kind}"
                        )
                verified_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(verified_emails)")
                }
                if "confirmation_count" not in verified_columns:
                    connection.execute(
                        "ALTER TABLE verified_emails ADD COLUMN confirmation_count "
                        "INTEGER NOT NULL DEFAULT 1"
                    )
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS verification_probe_leases (
                        email TEXT PRIMARY KEY, owner_job_id TEXT NOT NULL,
                        acquired_at TEXT NOT NULL, expires_at TEXT NOT NULL
                    )
                """)
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS verification_probe_waiters (
                        job_id TEXT NOT NULL, result_index INTEGER NOT NULL,
                        email TEXT NOT NULL, owner_job_id TEXT NOT NULL,
                        created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                        PRIMARY KEY(job_id, result_index)
                    )
                """)
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS verification_cache_days (
                        day TEXT PRIMARY KEY, lookups INTEGER NOT NULL DEFAULT 0,
                        fresh_hits INTEGER NOT NULL DEFAULT 0, misses INTEGER NOT NULL DEFAULT 0,
                        stale_seen INTEGER NOT NULL DEFAULT 0,
                        writes_deliverable INTEGER NOT NULL DEFAULT 0,
                        writes_permanent_invalid INTEGER NOT NULL DEFAULT 0,
                        writes_mailbox_full INTEGER NOT NULL DEFAULT 0,
                        coalesced_waiters INTEGER NOT NULL DEFAULT 0,
                        refresh_scheduled INTEGER NOT NULL DEFAULT 0,
                        updated_at TEXT NOT NULL
                    )
                """)
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_verification_probe_waiters_email "
                    "ON verification_probe_waiters(email)"
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
            self._initialized = True

    @staticmethod
    def _result_state(result: dict[str, Any]) -> str:
        state = str(result.get("progress_state") or "").lower()
        if state in {"pending", "verifying", "completed", "failed", "stopped"}:
            return state
        return "completed"

    def service_mode(self) -> str:
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT value FROM service_state WHERE name='verification_mode'"
            ).fetchone()
        return str(row[0]) if row and row[0] in {"active", "draining"} else "active"

    def service_state_value(self, name: str) -> str | None:
        """Read a small durable coordination value shared by all workers."""
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT value FROM service_state WHERE name=?", (name,)
            ).fetchone()
        return str(row[0]) if row and row[0] is not None else None

    def set_service_state_value(self, name: str, value: str) -> None:
        """Upsert a small durable coordination value without a schema migration."""
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO service_state(name, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (name, value, _sql_ts(utc_now())),
            )
            connection.commit()

    def database_ready(self) -> bool:
        """Check database reachability without scanning active result rows."""
        self.initialize()
        with closing(self._connect()) as connection:
            connection.execute("SELECT 1").fetchone()
        return True

    def health_summary(self) -> dict[str, object]:
        """Return inexpensive readiness signals without hydrating task payloads."""
        self.initialize()
        now = utc_now()
        stale_before_dt = now - timedelta(seconds=settings.worker_lease_seconds)
        stale_before = stale_before_dt.isoformat()
        with closing(self._connect()) as connection:
            mode_row = connection.execute(
                "SELECT value FROM service_state WHERE name='verification_mode'"
            ).fetchone()
            if postgres_active():
                job_counts = connection.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE status = 'queued'),
                        COUNT(*) FILTER (WHERE status = 'running')
                    FROM jobs
                    """
                ).fetchone()
                queued_n = int(job_counts[0] or 0)
                running_n = int(job_counts[1] or 0)
                # Skip scanning job_results (hundreds of thousands of rows) when idle.
                if queued_n == 0 and running_n == 0:
                    result_counts = (0, 0)
                else:
                    result_counts = connection.execute(
                        """
                        SELECT
                            COUNT(*) FILTER (WHERE r.progress_state = 'pending'),
                            COUNT(*) FILTER (WHERE r.progress_state = 'verifying')
                        FROM jobs j
                        JOIN job_results r ON r.job_id = j.id
                        WHERE j.status IN ('queued', 'running')
                        """
                    ).fetchone()
                stale_leases = connection.execute(
                    "SELECT COUNT(*) FROM job_leases WHERE completed_at IS NULL AND heartbeat_at < %s",
                    (stale_before_dt,),
                ).fetchone()[0]
            else:
                job_counts = connection.execute(
                    """
                    SELECT
                        COALESCE(SUM(status='queued'), 0),
                        COALESCE(SUM(status='running'), 0)
                    FROM jobs
                    """
                ).fetchone()
                result_counts = connection.execute(
                    """
                    SELECT
                        COALESCE(SUM(r.progress_state='pending'), 0),
                        COALESCE(SUM(r.progress_state='verifying'), 0)
                    FROM job_results r
                    JOIN jobs j ON j.id=r.job_id
                    WHERE j.status IN ('queued', 'running')
                    """
                ).fetchone()
                stale_leases = connection.execute(
                    "SELECT COUNT(*) FROM job_leases WHERE completed_at IS NULL AND heartbeat_at < ?",
                    (stale_before,),
                ).fetchone()[0]
            unhealthy_targets = [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT DISTINCT j.execution_target
                    FROM jobs j
                    WHERE j.status IN ('queued', 'running')
                      AND j.execution_target NOT IN ('local', 'aggregate')
                      AND NOT EXISTS (
                          SELECT 1 FROM worker_nodes n
                          WHERE n.target=j.execution_target AND n.health='healthy'
                      )
                    ORDER BY j.execution_target
                    """
                ).fetchall()
            ]
            profile_rows = connection.execute(
                """SELECT scheduler_key, current_limit, success_streak, pressure_events,
                          cooldown_until
                   FROM scheduler_domain_profiles"""
            ).fetchall()
            public_profile_rows = connection.execute(
                """SELECT scheduler_key, current_limit, success_streak, pressure_events,
                          cooldown_until
                   FROM scheduler_domain_profiles
                   WHERE scheduler_key IN ('gmail', 'microsoft')"""
            ).fetchall()
            recent_cutoff = _sql_ts(now - timedelta(seconds=60))
            recent_rows = connection.execute(
                """SELECT email, result_json
                   FROM job_results
                   WHERE progress_state IN ('completed', 'failed')
                     AND updated_at >= ?""",
                (recent_cutoff,),
            ).fetchall()
            active_slots = {
                str(key): int(slots or 0)
                for key, slots in connection.execute(
                    """SELECT mx_key, SUM(slots)
                       FROM mx_scheduler_leases
                       WHERE expires_at >= ? AND mx_key IN ('gmail', 'microsoft')
                       GROUP BY mx_key""",
                    (_sql_ts(now),),
                )
            }
            lease_ages: dict[str, list[float]] = {"gmail": [], "microsoft": []}
            for key, claimed_at in connection.execute(
                """SELECT mx.mx_key, lease.claimed_at
                   FROM mx_scheduler_leases mx
                   JOIN job_leases lease ON lease.id=mx.lease_id
                   WHERE mx.expires_at >= ? AND lease.completed_at IS NULL
                     AND mx.mx_key IN ('gmail', 'microsoft')""",
                (_sql_ts(now),),
            ):
                claimed = _dt(claimed_at)
                if claimed is not None:
                    lease_ages[str(key)].append(max(0.0, (now - claimed).total_seconds()))
        mode = str(mode_row[0]) if mode_row and mode_row[0] in {"active", "draining"} else "active"
        scheduler_profiles = {
            "tracked": len(profile_rows),
            "restricted": sum(
                int(limit) < self._scheduler_mx_capacity(str(key))
            for key, limit, _streak, _pressure, _cooldown in profile_rows
            ),
            "cooling": sum(
                1
            for _key, _limit, _streak, _pressure, cooldown in profile_rows
                if (parsed := _dt(cooldown)) is not None and parsed > now
            ),
        }
        profile_by_key = {
            str(key): {
                "limit": int(limit),
                "configured_limit": self._scheduler_mx_capacity(str(key)),
                "success_streak": int(streak),
                "pressure_events": int(pressure),
                "cooldown_until": _dt(cooldown).isoformat() if _dt(cooldown) else None,
            }
            for key, limit, streak, pressure, cooldown in public_profile_rows
        }
        recent_outcomes = {
            key: {"completed": 0, "pressure": 0, "timings": {}}
            for key in ("gmail", "microsoft")
        }
        for email, raw_result in recent_rows:
            key = self._scheduler_mx_key(str(email))
            if key not in recent_outcomes:
                continue
            recent_outcomes[key]["completed"] += 1
            try:
                result = _json_load(raw_result)
            except (TypeError, json.JSONDecodeError):
                result = None
            if isinstance(result, dict) and self._scheduler_pressure_signal(result):
                recent_outcomes[key]["pressure"] += 1
            timings = result.get("timings_ms") if isinstance(result, dict) else None
            if isinstance(timings, dict):
                for name, raw_value in timings.items():
                    if not isinstance(raw_value, (int, float)) or raw_value < 0:
                        continue
                    recent_outcomes[key]["timings"].setdefault(str(name), []).append(float(raw_value))

        def timing_summary(values: list[float]) -> dict[str, float | int]:
            ordered = sorted(values)
            p50_index = ((len(ordered) * 50 + 99) // 100) - 1
            p95_index = ((len(ordered) * 95 + 99) // 100) - 1
            return {
                "count": len(ordered),
                "p50": round(ordered[p50_index], 2),
                "p95": round(ordered[p95_index], 2),
            }

        scheduler_runtime = {}
        for key in ("gmail", "microsoft"):
            ages = sorted(lease_ages[key])
            p95 = ages[((len(ages) * 95 + 99) // 100) - 1] if ages else None
            scheduler_runtime[key] = {
                **profile_by_key.get(key, {
                    "limit": self._scheduler_mx_capacity(key),
                    "configured_limit": self._scheduler_mx_capacity(key),
                    "success_streak": 0,
                    "pressure_events": 0,
                    "cooldown_until": None,
                }),
                "completed_last_60_seconds": recent_outcomes[key]["completed"],
                "pressure_last_60_seconds": recent_outcomes[key]["pressure"],
                "timings_ms_last_60_seconds": {
                    name: timing_summary(values)
                    for name, values in recent_outcomes[key]["timings"].items()
                    if values
                },
                "active_slots": active_slots.get(key, 0),
                "active_lease_age_p95_seconds": round(p95, 2) if p95 is not None else None,
            }
        return {
            "service_mode": mode,
            "queued_jobs": int(job_counts[0]),
            "running_jobs": int(job_counts[1]),
            "pending_results": int(result_counts[0]),
            "verifying_results": int(result_counts[1]),
            "stale_leases": int(stale_leases),
            "unhealthy_targets": unhealthy_targets,
            "scheduler_profiles": scheduler_profiles,
            "scheduler_runtime": scheduler_runtime,
        }

    def set_service_mode(self, mode: str) -> None:
        if mode not in {"active", "draining"}:
            raise ValueError("Unsupported verification service mode")
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO service_state(name, value, updated_at) VALUES ('verification_mode', ?, ?)
                ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (mode, utc_now().isoformat()),
            )

    @classmethod
    def _result_row(cls, job_id: str, index: int, result: dict[str, Any], now: str) -> tuple[Any, ...]:
        """Return one durable result row, including fields needed by list queries.

        Boolean flags use Python ``bool`` so PostgreSQL ``boolean`` columns accept
        them; SQLite stores True/False as 1/0 in INTEGER columns.
        """
        payload = dict(result)
        payload["original_index"] = index
        deliverable = payload.get("deliverable")
        state = cls._result_state(payload)
        return (
            job_id,
            index,
            str(payload.get("email") or ""),
            state,
            json.dumps(payload, ensure_ascii=False, default=str),
            now,
            now if state in {"completed", "failed", "stopped"} else None,
            1 if deliverable is True else 0 if deliverable is False else None,
            bool(payload.get("valid") is True),
            bool(payload.get("skipped") is True),
            bool(payload.get("domain_type") == "catch-all"),
            str(payload.get("retry_at") or "") or None,
            bool(payload.get("retry_updated") is True),
            True,
        )

    def _backfill_result_query_fields(self, connection: sqlite3.Connection) -> int:
        """Populate query columns from legacy JSON without changing result payloads."""
        rows = connection.execute(
            """SELECT job_id, original_index, result_json, updated_at FROM job_results
            WHERE query_fields_ready=0"""
        ).fetchall()
        updates: list[tuple[Any, ...]] = []
        for job_id, index, payload, updated_at in rows:
            try:
                result = _json_load(payload)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(result, dict):
                continue
            row = self._result_row(str(job_id), int(index), result, str(updated_at))
            updates.append((*row[7:], str(job_id), int(index)))
        if not updates:
            return 0
        connection.executemany(
            """UPDATE job_results SET deliverability=?, is_valid=?, is_skipped=?, is_catch_all=?,
            retry_at=?, retry_updated=?, query_fields_ready=? WHERE job_id=? AND original_index=?""",
            updates,
        )
        return len(updates)

    def _backfill_result_rows(self, connection: sqlite3.Connection) -> None:
        """Migrate legacy JSON snapshots once, without overwriting newer result rows."""
        if connection.execute("SELECT 1 FROM job_results LIMIT 1").fetchone() is not None:
            return
        now = utc_now().isoformat()
        for job_id, emails_json, results_json in connection.execute(
            "SELECT id, emails_json, results_json FROM jobs"
        ):
            try:
                emails, results = _json_load(emails_json), _json_load(results_json)
            except json.JSONDecodeError:
                continue
            indexed = {int(item.get("original_index", index)): item for index, item in enumerate(results) if isinstance(item, dict)}
            rows = []
            for index, email in enumerate(emails):
                result = dict(indexed.get(index, {"email": email, "original_index": index, "progress_state": "pending"}))
                result["email"], result["original_index"] = str(result.get("email") or email), index
                rows.append(self._result_row(job_id, index, result, now))
            if rows:
                connection.executemany("""
                    INSERT INTO job_results(job_id, original_index, email, progress_state, result_json, updated_at,
                        initial_completed_at, deliverability, is_valid, is_skipped, is_catch_all,
                        retry_at, retry_updated, query_fields_ready)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id, original_index) DO NOTHING
                """, rows)

    def migrate_legacy_results(self) -> dict[str, int]:
        """Explicitly migrate JSON snapshots; never run this in web startup."""
        self.initialize()
        migrated_rows = 0
        linked_rows = 0
        with self._lock, closing(self._connect()) as connection:
            begin_immediate(connection)
            now = utc_now().isoformat()
            for job_id, emails_json, results_json in connection.execute(
                "SELECT id, emails_json, results_json FROM jobs"
            ):
                try:
                    emails = _json_load(emails_json)
                    results = _json_load(results_json or "[]")
                except json.JSONDecodeError:
                    continue
                indexed = {
                    int(item.get("original_index", index)): item
                    for index, item in enumerate(results) if isinstance(item, dict)
                }
                rows = []
                for index, email in enumerate(emails):
                    result = dict(indexed.get(index, {
                        "email": email, "original_index": index, "progress_state": "pending",
                    }))
                    result["email"] = str(result.get("email") or email)
                    result["original_index"] = index
                    rows.append(self._result_row(job_id, index, result, now))
                cursor = connection.executemany("""
                    INSERT INTO job_results(job_id, original_index, email, progress_state, result_json, updated_at,
                        initial_completed_at, deliverability, is_valid, is_skipped, is_catch_all,
                        retry_at, retry_updated, query_fields_ready)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id, original_index) DO NOTHING
                """, rows)
                migrated_rows += max(0, cursor.rowcount)

            parents = connection.execute(
                "SELECT id, emails_json FROM jobs WHERE execution_target='aggregate'"
            ).fetchall()
            for parent_id, parent_emails_json in parents:
                try:
                    parent_emails = _json_load(parent_emails_json)
                except json.JSONDecodeError:
                    continue
                positions: dict[str, list[int]] = {}
                for index, email in enumerate(parent_emails):
                    positions.setdefault(str(email).lower(), []).append(index)
                consumed: dict[str, int] = {}
                links = []
                for child_id, child_emails_json in connection.execute(
                    "SELECT id, emails_json FROM jobs WHERE parent_id=? ORDER BY created_at, id", (parent_id,)
                ):
                    try:
                        child_emails = _json_load(child_emails_json)
                    except json.JSONDecodeError:
                        continue
                    for child_index, email in enumerate(child_emails):
                        key = str(email).lower()
                        offset = consumed.get(key, 0)
                        candidates = positions.get(key, [])
                        if offset < len(candidates):
                            links.append((child_id, child_index, parent_id, candidates[offset]))
                            consumed[key] = offset + 1
                cursor = connection.executemany("""
                    INSERT INTO job_result_links(child_job_id, child_index, parent_job_id, parent_index)
                    VALUES (?, ?, ?, ?) ON CONFLICT(child_job_id, child_index) DO NOTHING
                """, links)
                linked_rows += max(0, cursor.rowcount)
            connection.execute(
                "INSERT OR REPLACE INTO schema_migrations(name, applied_at) VALUES ('legacy_result_rows_v1', ?)",
                (now,),
            )
            query_fields = self._backfill_result_query_fields(connection)
            connection.commit()
        return {
            "result_rows": migrated_rows,
            "result_links": linked_rows,
            "result_query_fields": query_fields,
        }

    def release_legacy_deferred_retries(self) -> int:
        """Release only tasks queued by the retired multi-minute retry policy."""
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            return connection.execute(
                """
                UPDATE jobs SET deferred_retry_at = NULL, temporary_retry_attempts = 0
                WHERE status = 'queued' AND deferred_retry_at IS NOT NULL AND error LIKE ?
                """,
                ("%自动复核%",),
            ).rowcount

    def clear_completed_retry_notices(self) -> int:
        """Remove obsolete retry notices after a repaired task has completed."""
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            return connection.execute(
                """
                UPDATE jobs SET error = NULL
                WHERE status = 'completed' AND error LIKE ?
                """,
                ("检测到未完成的 SMTP 临时结果%",),
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
                    checks = _json_load(result_json).get("checks") or {}
                except json.JSONDecodeError:
                    continue
                if checks.get("domain") is False or checks.get("mx") is False:
                    stale.append((email,))
            if stale:
                connection.executemany(
                    "DELETE FROM verification_cache WHERE email=?", stale
                )
            return len(stale)

    @staticmethod
    def _job_values(job: Job) -> tuple[Any, ...]:
        return (
            job.id,
            json.dumps(job.emails, ensure_ascii=False),
            job.worker_count,
            job.status,
            _sql_ts(job.created_at),
            _sql_ts(job.started_at),
            _sql_ts(job.finished_at),
            job.error,
            "[]",
            str(job.csv_path) if job.csv_path else None,
            job.owner_id,
            job.guest_token_hash,
            job.worker_id,
            _sql_ts(job.heartbeat_at),
            # bool works for SQLite INTEGER and PostgreSQL boolean columns.
            bool(job.stop_on_deliverable),
            job.execution_target,
            job.parent_id,
            job.retry_parent_id,
            _sql_ts(job.deferred_retry_at),
            job.temporary_retry_attempts,
            job.retry_route,
            job.origin_execution_target,
            job.cross_route_attempts,
            job.list_name,
            bool(job.is_cache_refresh),
        )
    def _persist_metadata(self, connection: sqlite3.Connection, job: Job) -> None:
        connection.execute(
            """
            INSERT INTO jobs (
                id, emails_json, worker_count, status, created_at, started_at, finished_at,
                error, results_json, csv_path, owner_id, guest_token_hash, worker_id, heartbeat_at,
                stop_on_deliverable, execution_target, parent_id, retry_parent_id, deferred_retry_at,
                temporary_retry_attempts, retry_route, origin_execution_target, cross_route_attempts,
                list_name, is_cache_refresh
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                temporary_retry_attempts=excluded.temporary_retry_attempts,
                retry_route=excluded.retry_route,
                origin_execution_target=excluded.origin_execution_target,
                cross_route_attempts=excluded.cross_route_attempts,
                list_name=excluded.list_name,
                is_cache_refresh=excluded.is_cache_refresh
            WHERE jobs.status != 'stopped' OR excluded.status = 'stopped'
            """,
            self._job_values(job),
        )

    def _ensure_result_rows(self, connection: sqlite3.Connection, job: Job) -> None:
        if not job.emails:
            return
        now = utc_now().isoformat()
        rows = [
            self._result_row(
                job.id,
                index,
                {"email": email, "original_index": index, "progress_state": "pending"},
                now,
            )
            for index, email in enumerate(job.emails)
        ]
        connection.executemany("""
            INSERT INTO job_results(job_id, original_index, email, progress_state, result_json, updated_at,
                initial_completed_at, deliverability, is_valid, is_skipped, is_catch_all,
                retry_at, retry_updated, query_fields_ready)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, original_index) DO NOTHING
        """, rows)

    def add(self, job: Job, max_active: int | None = None) -> None:
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            begin_immediate(connection)
            active = connection.execute(
                """SELECT COUNT(*) FROM jobs
                WHERE status IN ('queued', 'running')
                    AND parent_id IS NULL AND retry_parent_id IS NULL
                    AND is_cache_refresh = 0"""
            ).fetchone()[0]
            if job.is_cache_refresh and job.parent_id is None:
                refresh_active = connection.execute("""
                    SELECT COUNT(*) FROM jobs
                    WHERE status IN ('queued', 'running') AND is_cache_refresh = 1
                        AND parent_id IS NULL
                """).fetchone()[0]
                if refresh_active >= settings.verification_cache_refresh_max_queued:
                    connection.rollback()
                    raise RuntimeError("Verification cache refresh queue is full")
            if max_active is not None and active >= max_active:
                connection.rollback()
                raise RuntimeError("任务队列已满，请等待已有任务完成")
            self._persist_metadata(connection, job)
            if job.results:
                self._upsert_results(connection, job.id, job.results)
            else:
                self._ensure_result_rows(connection, job)
            connection.commit()

    def persist(self, job: Job) -> None:
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            begin_immediate(connection)
            self._persist_metadata(connection, job)
            if job.results:
                self._upsert_results(connection, job.id, job.results)
            else:
                self._ensure_result_rows(connection, job)
            connection.commit()

    def persist_worker_progress(self, job: Job, worker_id: str) -> bool:
        """Persist result progress only while this worker still owns a running job.

        Worker objects are intentionally stale snapshots.  A conditional update
        prevents a stopped job or a newly reclaimed job from being overwritten
        by an older worker callback.
        """
        self.initialize()
        now = utc_now()
        with self._lock, closing(self._connect()) as connection:
            begin_immediate(connection)
            changed = connection.execute(
                """
                UPDATE jobs SET started_at=COALESCE(started_at, ?), heartbeat_at=?
                WHERE id=? AND status='running' AND worker_id=?
                """,
                (
                    _sql_ts(job.started_at or now),
                    _sql_ts(now),
                    job.id,
                    worker_id,
                ),
            ).rowcount
            if not changed:
                connection.rollback()
                return False
            if job.results:
                self._upsert_results(connection, job.id, job.results)
            connection.commit()
        job.heartbeat_at = now
        return True

    def clear_worker_runtime(self, job_id: str, worker_id: str) -> bool:
        """Release runtime ownership without changing the job's business state."""
        self.initialize()
        now = utc_now()
        heartbeat = now if postgres_active() else _sql_ts(now)
        with self._lock, closing(self._connect()) as connection:
            changed = connection.execute(
                """
                UPDATE jobs SET worker_id=NULL,
                    heartbeat_at=CASE WHEN status='running' THEN ? ELSE NULL END
                WHERE id=? AND status='running' AND worker_id=?
                """,
                (heartbeat, job_id, worker_id),
            ).rowcount
        return bool(changed)

    def ensure_result_rows(self, job: Job) -> None:
        """Support legacy callers that created a job before visible waiting rows."""
        with self._lock, closing(self._connect()) as connection:
            begin_immediate(connection)
            self._ensure_result_rows(connection, job)
            connection.commit()

    def link_child_results(self, child_job_id: str, parent_job_id: str, parent_indices: list[int]) -> None:
        """Attach child-local indexes to their immutable parent result slots."""
        if not parent_indices:
            return
        self.initialize()
        rows = [
            (child_job_id, child_index, parent_job_id, parent_index)
            for child_index, parent_index in enumerate(parent_indices)
        ]
        with self._lock, closing(self._connect()) as connection:
            connection.executemany("""
                INSERT INTO job_result_links(child_job_id, child_index, parent_job_id, parent_index)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(child_job_id, child_index) DO UPDATE SET
                    parent_job_id=excluded.parent_job_id, parent_index=excluded.parent_index
            """, rows)

    def results_for_job(self, job_id: str) -> list[dict[str, Any]]:
        self.initialize()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT progress_state, result_json FROM job_results WHERE job_id=? ORDER BY original_index", (job_id,)
            ).fetchall()
        results = []
        for state, raw in rows:
            result = _json_load(raw)
            result["progress_state"] = state
            results.append(result)
        return results

    def result_page(
        self,
        job_id: str,
        *,
        offset: int,
        limit: int,
        search: str = "",
        deliverability: str = "all",
    ) -> tuple[int, list[dict[str, Any]]]:
        """Fetch one visible result page without hydrating the whole task."""
        self.initialize()
        clauses = ["job_id=?"]
        parameters: list[Any] = [job_id]
        if search:
            clauses.append("email LIKE ? COLLATE NOCASE")
            parameters.append(f"%{search}%")
        if deliverability == "deliverable":
            clauses.append("deliverability=1")
        elif deliverability == "undeliverable":
            clauses.append("deliverability=0")
        elif deliverability == "unknown":
            clauses.append("deliverability IS NULL AND is_skipped=0")
        where = " AND ".join(clauses)
        with closing(self._connect()) as connection:
            available = int(connection.execute(
                f"SELECT COUNT(*) FROM job_results WHERE {where}", parameters
            ).fetchone()[0])
            rows = connection.execute(
                f"""SELECT progress_state, result_json FROM job_results WHERE {where}
                ORDER BY original_index LIMIT ? OFFSET ?""",
                [*parameters, limit, offset],
            ).fetchall()
        results = []
        for state, raw in rows:
            result = _json_load(raw)
            result["progress_state"] = state
            results.append(result)
        return available, results

    def clear_result_review_update(self, job_id: str, result_index: int) -> bool:
        """Clear the unread-review marker for one result, never its siblings."""
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT result_json FROM job_results WHERE job_id=? AND original_index=?",
                (job_id, result_index),
            ).fetchone()
            if row is None:
                return False
            result = _json_load(row[0])
            if not result.pop("retry_updated", None):
                return True
            connection.execute(
                "UPDATE job_results SET result_json=?, retry_updated=0 WHERE job_id=? AND original_index=?",
                (json.dumps(result, ensure_ascii=False, default=str), job_id, result_index),
            )
        return True

    def clear_job_review_updates(self, job_id: str) -> int:
        """Clear all review markers without rewriting task metadata."""
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT original_index, result_json FROM job_results WHERE job_id=? AND retry_updated=1",
                (job_id,),
            ).fetchall()
            if not rows:
                return 0
            updates = []
            for result_index, raw in rows:
                try:
                    result = _json_load(raw)
                except (TypeError, json.JSONDecodeError):
                    result = {}
                result.pop("retry_updated", None)
                updates.append((json.dumps(result, ensure_ascii=False, default=str), job_id, int(result_index)))
            connection.executemany(
                "UPDATE job_results SET result_json=?, retry_updated=0 WHERE job_id=? AND original_index=?",
                updates,
            )
        return len(updates)

    def result_overview(self, job_id: str) -> ResultOverview:
        """Return the task counters used by status polling without loading result JSON."""
        self.initialize()
        if postgres_active():
            valid_sql = "CASE WHEN is_valid IS TRUE THEN 1 ELSE 0 END"
            skip_false = "(is_skipped IS NOT TRUE)"
            catch_sql = "CASE WHEN is_catch_all IS TRUE THEN 1 ELSE 0 END"
            retry_sql = "CASE WHEN retry_updated IS TRUE THEN 1 ELSE 0 END"
        else:
            valid_sql = "CASE WHEN is_valid=1 THEN 1 ELSE 0 END"
            skip_false = "(COALESCE(is_skipped, 0)=0)"
            catch_sql = "CASE WHEN is_catch_all=1 THEN 1 ELSE 0 END"
            retry_sql = "CASE WHEN retry_updated=1 THEN 1 ELSE 0 END"
        with closing(self._connect()) as connection:
            row = connection.execute(
                f"""SELECT
                    COUNT(*),
                    COALESCE(SUM(CASE WHEN progress_state NOT IN ('pending', 'verifying') THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM({valid_sql}), 0),
                    COALESCE(SUM(CASE WHEN deliverability=1 THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN deliverability=0 THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN deliverability IS NULL AND {skip_false} THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM({catch_sql}), 0),
                    MIN(retry_at),
                    COALESCE(MAX({retry_sql}), 0)
                FROM job_results WHERE job_id=?""",
                (job_id,),
            ).fetchone()
        retry_at = None
        if row[7]:
            try:
                retry_at = _dt(str(row[7]))
            except ValueError:
                pass
        return ResultOverview(
            settled=int(row[1]), total=int(row[0]), valid=int(row[2]),
            deliverable=int(row[3]), undeliverable=int(row[4]), unknown=int(row[5]),
            catch_all=int(row[6]), retry_at=retry_at, review_updated=bool(row[8]),
        )

    def earliest_child_retry(self, parent_id: str) -> datetime | None:
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT MIN(deferred_retry_at) FROM jobs
                WHERE parent_id=? AND deferred_retry_at IS NOT NULL""",
                (parent_id,),
            ).fetchone()
        return _dt(row[0]) if row and row[0] else None

    def _upsert_results(
        self, connection: sqlite3.Connection, job_id: str, results: list[dict[str, Any]],
    ) -> None:
        if not results:
            return
        now = utc_now().isoformat()
        rows = []
        for fallback_index, raw in enumerate(results):
            result = dict(raw)
            index = int(result.get("original_index", fallback_index))
            result["original_index"] = index
            email = str(result.get("email") or "")
            rows.append(self._result_row(job_id, index, result, now))
        connection.executemany("""
                INSERT INTO job_results(job_id, original_index, email, progress_state, result_json, updated_at,
                    initial_completed_at, deliverability, is_valid, is_skipped, is_catch_all,
                    retry_at, retry_updated, query_fields_ready)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id, original_index) DO UPDATE SET
                    email=excluded.email, progress_state=excluded.progress_state,
                    result_json=excluded.result_json, updated_at=excluded.updated_at,
                    initial_completed_at=COALESCE(job_results.initial_completed_at, excluded.initial_completed_at),
                    deliverability=excluded.deliverability, is_valid=excluded.is_valid,
                    is_skipped=excluded.is_skipped, is_catch_all=excluded.is_catch_all,
                    retry_at=excluded.retry_at, retry_updated=excluded.retry_updated,
                    query_fields_ready=excluded.query_fields_ready
                WHERE job_results.progress_state IN ('pending', 'verifying')
                    OR (job_results.progress_state = excluded.progress_state
                        AND job_results.result_json <> excluded.result_json)
        """, rows)
        links = connection.execute("""
                SELECT child_index, parent_job_id, parent_index FROM job_result_links
                WHERE child_job_id=?
        """, (job_id,)).fetchall()
        if links:
            source = {int(row[1]): (row[2], row[3], row[4]) for row in rows}
            parent_rows = []
            for child_index, parent_id, parent_index in links:
                item = source.get(int(child_index))
                if item is None:
                    continue
                _email, _state, payload = item
                result = _json_load(payload)
                result["original_index"] = int(parent_index)
                parent_rows.append(self._result_row(parent_id, int(parent_index), result, now))
            connection.executemany("""
                    INSERT INTO job_results(job_id, original_index, email, progress_state, result_json, updated_at,
                        initial_completed_at, deliverability, is_valid, is_skipped, is_catch_all,
                        retry_at, retry_updated, query_fields_ready)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id, original_index) DO UPDATE SET
                        email=excluded.email, progress_state=excluded.progress_state,
                        result_json=excluded.result_json, updated_at=excluded.updated_at,
                        initial_completed_at=COALESCE(job_results.initial_completed_at, excluded.initial_completed_at),
                        deliverability=excluded.deliverability, is_valid=excluded.is_valid,
                        is_skipped=excluded.is_skipped, is_catch_all=excluded.is_catch_all,
                        retry_at=excluded.retry_at, retry_updated=excluded.retry_updated,
                        query_fields_ready=excluded.query_fields_ready
                    WHERE job_results.progress_state IN ('pending', 'verifying')
                        OR (job_results.progress_state = excluded.progress_state
                            AND job_results.result_json <> excluded.result_json)
            """, parent_rows)

    def upsert_results(self, job_id: str, results: list[dict[str, Any]]) -> None:
        """Write result deltas in one transaction; terminal rows cannot regress."""
        if not results:
            return
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            begin_immediate(connection)
            self._upsert_results(connection, job_id, results)
            connection.commit()

    def _reconcile_catch_all_conflicts_in_connection(
        self, connection: sqlite3.Connection, job_id: str,
    ) -> int:
        """Reconcile one job while the caller owns the surrounding transaction."""
        from app.core.catch_all import catch_all_domains, reconcile_catch_all_conflicts

        rows = connection.execute(
            "SELECT original_index, result_json FROM job_results WHERE job_id=?",
            (job_id,),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for index, raw in rows:
            try:
                result = _json_load(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(result, dict):
                continue
            result["original_index"] = int(index)
            results.append(result)
        conflicts = reconcile_catch_all_conflicts(results)
        domains = catch_all_domains(results)
        if domains:
            placeholders = ", ".join("?" for _ in domains)
            domain_expression = (
                "lower(split_part(email, '@', 2))"
                if postgres_active()
                else "lower(substr(email, instr(email, '@') + 1))"
            )
            evidence_rows = connection.execute(
                f"""SELECT result_json FROM job_results
                WHERE deliverability=0
                AND {domain_expression} IN ({placeholders})""",
                tuple(sorted(domains)),
            ).fetchall()
            evidence: list[dict[str, Any]] = []
            for (raw,) in evidence_rows:
                try:
                    result = _json_load(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(result, dict):
                    evidence.append(result)
            conflicts |= reconcile_catch_all_conflicts([*results, *evidence])
        if not conflicts:
            return 0
        now = utc_now().isoformat()
        updated_rows = [
            self._result_row(job_id, int(result["original_index"]), result, now)
            for result in results
            if str(result.get("email") or "").rsplit("@", 1)[-1].lower() in conflicts
        ]
        connection.executemany(
            """UPDATE job_results SET result_json=?, updated_at=?, deliverability=?, is_valid=?,
            is_skipped=?, is_catch_all=?, retry_at=?, retry_updated=?, query_fields_ready=?
            WHERE job_id=? AND original_index=?""",
            [
                (
                    row[4], row[5], row[7], row[8], row[9], row[10], row[11], row[12], row[13],
                    row[0], row[1],
                )
                for row in updated_rows
            ],
        )
        placeholders = ", ".join("?" for _ in conflicts)
        connection.execute(
            f"DELETE FROM catch_all_emails WHERE job_id=? AND lower(domain) IN ({placeholders})",
            (job_id, *sorted(conflicts)),
        )
        return len(updated_rows)

    def reconcile_catch_all_conflicts(self, job_id: str) -> int:
        """Keep a domain from exposing Catch-all beside contradictory SMTP verdicts."""
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            begin_immediate(connection)
            changed = self._reconcile_catch_all_conflicts_in_connection(connection, job_id)
            connection.commit()
        return changed

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

    def list_recent(
        self, owner_id: str, limit: int = 20, *, include_results: bool = False
    ) -> list[Job]:
        self.initialize()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT {self._select_columns()} FROM jobs WHERE owner_id = ? AND parent_id IS NULL AND retry_parent_id IS NULL ORDER BY created_at DESC LIMIT ?",
                (owner_id, limit),
            ).fetchall()
        jobs = [self._job_from_row(row) for row in rows]
        return [self._hydrate_results(job) for job in jobs] if include_results else jobs

    def page_for_owner(
        self, owner_id: str, *, offset: int = 0, limit: int = 20,
        search: str = "", status: str = "all",
    ) -> tuple[int, list[Job]]:
        """Return paginated top-level task history without hydrating results."""
        self.initialize()
        with closing(self._connect()) as connection:
            clauses = ["owner_id=?", "parent_id IS NULL", "retry_parent_id IS NULL"]
            params: list[object] = [owner_id]
            if status in {"queued", "running", "completed", "failed", "stopped"}:
                clauses.append("status=?")
                params.append(status)
            if search.strip():
                emails_expr = "lower(emails_json::text)" if postgres_active() else "lower(emails_json)"
                clauses.append(
                    f"(lower(COALESCE(list_name, '')) LIKE ? OR {emails_expr} LIKE ? OR lower(COALESCE(csv_path, '')) LIKE ?)"
                )
                needle = f"%{search.strip().lower()}%"
                params.extend([needle, needle, needle])
            where = " AND ".join(clauses)
            total = int(connection.execute(
                f"SELECT COUNT(*) FROM jobs WHERE {where}", params
            ).fetchone()[0])
            rows = connection.execute(
                f"""SELECT {self._select_columns()} FROM jobs WHERE {where}
                ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                [*params, limit, offset],
            ).fetchall()
        return total, [self._job_from_row(row) for row in rows]

    def workspace_overview(
        self, owner_id: str, *, day_start: datetime, day_end: datetime,
    ) -> WorkspaceOverview:
        """Aggregate one owner's visible task history without per-job queries."""
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*),
                    COALESCE(SUM(CASE
                        WHEN j.created_at >= ? AND j.created_at < ?
                        THEN COALESCE(results.settled, 0) ELSE 0
                    END), 0),
                    COALESCE(SUM(results.deliverable), 0),
                    COALESCE(SUM(results.settled), 0)
                FROM jobs AS j
                LEFT JOIN (
                    SELECT
                        job_id,
                        COALESCE(SUM(CASE WHEN progress_state NOT IN ('pending', 'verifying') THEN 1 ELSE 0 END), 0) AS settled,
                        COALESCE(SUM(CASE WHEN deliverability=1 THEN 1 ELSE 0 END), 0) AS deliverable
                    FROM job_results
                    GROUP BY job_id
                ) AS results ON results.job_id = j.id
                WHERE j.owner_id=? AND j.parent_id IS NULL AND j.retry_parent_id IS NULL
                """,
                (day_start.isoformat(), day_end.isoformat(), owner_id),
            ).fetchone()
        return WorkspaceOverview(
            total=int(row[0]),
            processed_today=int(row[1]),
            deliverable=int(row[2]),
            settled=int(row[3]),
        )

    def recent_completed_single_jobs(self, since: datetime) -> list[Job]:
        """Return standalone single-address jobs eligible for a narrow repair pass."""
        self.initialize()
        emails_like = "emails_json::text" if postgres_active() else "emails_json"
        list_separator_pattern = "'%%,%%'" if postgres_active() else "'%,%'"
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""SELECT {self._select_columns()} FROM jobs
                WHERE status='completed' AND parent_id IS NULL AND execution_target != 'aggregate'
                    AND retry_parent_id IS NULL AND created_at >= ? AND {emails_like} NOT LIKE {list_separator_pattern}
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

    def orphaned_retry_parent_ids(self, cutoff: datetime, limit: int = 25) -> list[str]:
        """Return settled parent tasks whose visible recheck no longer has a worker job."""
        self.initialize()
        cutoff_bind = cutoff if postgres_active() else _sql_ts(cutoff)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT result.job_id, MIN(result.retry_at) AS oldest_retry
                FROM job_results AS result
                JOIN jobs AS parent ON parent.id=result.job_id
                WHERE result.retry_at IS NOT NULL AND result.retry_at <= ?
                    AND parent.parent_id IS NULL AND parent.retry_parent_id IS NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM jobs AS child
                        WHERE child.retry_parent_id=parent.id
                            AND child.status IN ('queued', 'running')
                    )
                GROUP BY result.job_id
                ORDER BY oldest_retry, result.job_id
                LIMIT ?
                """,
                (cutoff_bind, max(1, min(int(limit), 1000))),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def refresh_parent(self, parent_id: str) -> Job | None:
        """Merge child results into the user-visible parent task."""
        parent = self.get(parent_id, include_results=False)
        if parent is None or parent.status == "stopped":
            return parent
        with closing(self._connect()) as connection:
            children = connection.execute(
                "SELECT status, started_at, finished_at, error FROM jobs WHERE parent_id=? ORDER BY created_at, id",
                (parent_id,),
            ).fetchall()
        if not children:
            return parent

        started = [dt for row in children if row[1] for dt in [_dt(row[1])] if dt is not None]
        parent.started_at = min(started) if started else None
        terminal = {"completed", "failed", "stopped"}
        if all(row[0] in terminal for row in children):
            parent.finished_at = max(
                (_dt(row[2]) or utc_now() for row in children), default=utc_now()
            )
            failures = [row[3] for row in children if row[0] == "failed" and row[3]]
            if failures:
                parent.status = "failed"
                parent.error = "；".join(failures[:2])[:500]
                if parent.owner_id:
                    from app.db.auth import auth_store
                    auth_store.refund_failed_submission(parent.owner_id, parent.emails, f"verification:{parent.id}")
            elif any(row[0] == "stopped" for row in children):
                parent.status = "stopped"
                parent.error = "已由用户停止验证"
            else:
                parent.status = "completed"
                parent.error = None
        else:
            parent.status = "running"
            parent.finished_at = None
            notices = [row[3] for row in children if row[0] == "queued" and row[3]]
            parent.error = notices[0] if notices else None
        self.persist(parent)
        return self.get(parent_id, include_results=False)

    def claim_next(
        self,
        worker_id: str,
        execution_target: str = "local",
        *,
        stop_on_deliverable_only: bool = False,
    ) -> Job | None:
        """Atomically claim the next task; expired worker leases are returned to the queue."""
        self.initialize()
        now = utc_now()
        stale_before = now - timedelta(seconds=settings.worker_lease_seconds)
        with self._lock, closing(self._connect()) as connection:
            begin_immediate(connection)
            connection.execute(
                """
                UPDATE jobs SET status = 'queued', worker_id = NULL, heartbeat_at = NULL,
                    error = '工作节点已重新领取任务'
                WHERE status = 'running' AND heartbeat_at IS NOT NULL AND heartbeat_at < ?
                """,
                (_sql_ts(stale_before),),
            )
            # stop_on_deliverable uses INTEGER 0/1 on SQLite and boolean on PG.
            # Literal "= 1" / "= 0" are rewritten by pg_compat; avoid binding 0/1
            # into a boolean comparison.
            stop_clause = "AND stop_on_deliverable = 1" if stop_on_deliverable_only else ""
            row = connection.execute(
                f"""SELECT {self._select_columns()} FROM jobs
                WHERE status = 'queued' AND execution_target = ?
                    {stop_clause}
                    AND (deferred_retry_at IS NULL OR deferred_retry_at <= ?)
                ORDER BY is_cache_refresh ASC, created_at LIMIT 1""",
                (execution_target, _sql_ts(now)),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            job = self._hydrate_results(self._job_from_row(row))
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
                    deferred_retry_at = NULL, error = NULL
                WHERE id = ?
                """,
                (
                    worker_id, _sql_ts(job.started_at), _sql_ts(now),
                    job.id,
                ),
            )
            connection.commit()
        return job

    def claim_remote_lease(
        self, worker_id: str, execution_target: str, *, capacity: int = 1, shard_size: int = 100,
        allow_local_fallback: bool = False, prospecting_shard_size: int | None = None,
    ) -> Job | None:
        """Allocate unfinished indexes, prioritizing a remote node's own queue."""
        self.initialize()
        now = utc_now()
        stale = now - timedelta(seconds=settings.worker_lease_seconds)
        with self._lock, closing(self._connect()) as connection:
            begin_immediate(connection)
            if postgres_active():
                advisory = connection.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtext(?), hashtext(?))",
                    (execution_target, worker_id),
                ).fetchone()
                if not advisory or not bool(advisory[0]):
                    connection.rollback()
                    return None
            # Expired leases are released precisely below. Avoid a global
            # result scan on every long-poll claim while holding this write
            # transaction; the maintenance path retains the orphan sweep.
            expired = connection.execute("""
                SELECT id, job_id, indices_json FROM job_leases
                WHERE completed_at IS NULL AND heartbeat_at < ?
            """, (_sql_ts(stale),)).fetchall()
            for expired_lease_id, expired_job_id, raw_indices in expired:
                try:
                    expired_indices = [int(index) for index in _json_load(raw_indices)]
                except (TypeError, ValueError, json.JSONDecodeError):
                    expired_indices = []
                if expired_indices:
                    placeholders = ", ".join("?" for _ in expired_indices)
                    connection.execute(
                        f"UPDATE job_results SET progress_state='pending' WHERE job_id=? "
                        f"AND original_index IN ({placeholders}) AND progress_state='verifying'",
                        (expired_job_id, *expired_indices),
                    )
                connection.execute("UPDATE job_leases SET completed_at=? WHERE id=?", (_sql_ts(now), expired_lease_id))
                connection.execute("DELETE FROM mx_scheduler_leases WHERE lease_id=?", (expired_lease_id,))
            connection.execute("DELETE FROM mx_scheduler_leases WHERE expires_at < ?", (_sql_ts(now),))
            connection.execute("""
                INSERT INTO worker_nodes(target, worker_id, capacity, health, last_seen_at)
                VALUES (?, ?, ?, 'healthy', ?)
                ON CONFLICT(target, worker_id) DO UPDATE SET capacity=excluded.capacity,
                    health='healthy', last_seen_at=excluded.last_seen_at
            """, (execution_target, worker_id, max(1, capacity), _sql_ts(now)))
            load = connection.execute("""
                SELECT COUNT(*) FROM job_leases WHERE worker_id=? AND execution_target=?
                    AND completed_at IS NULL AND heartbeat_at >= ?
            """, (worker_id, execution_target, _sql_ts(stale))).fetchone()[0]
            if load >= max(1, capacity):
                connection.commit()
                return None
            claim_targets = [execution_target]
            if allow_local_fallback and execution_target != "local":
                claim_targets.append("local")
            target_placeholders = ", ".join("?" for _ in claim_targets)
            epoch = (
                "TIMESTAMPTZ '1970-01-01 00:00:00+00'"
                if postgres_active()
                else "'1970-01-01T00:00:00+00:00'"
            )
            rows = connection.execute(f"""
                SELECT {', '.join('j.' + column for column in self._columns)} FROM jobs j
                LEFT JOIN jobs parent ON parent.id=j.parent_id
                LEFT JOIN scheduler_owner_turns turn ON turn.target=?
                    AND turn.owner_key=COALESCE(parent.owner_id, j.owner_id, j.id)
                WHERE j.status IN ('queued', 'running') AND j.execution_target IN ({target_placeholders})
                    AND j.stop_on_deliverable = 0
                    AND (j.deferred_retry_at IS NULL OR j.deferred_retry_at <= ?)
                    AND EXISTS (SELECT 1 FROM job_results r WHERE r.job_id=j.id
                        AND r.progress_state='pending'
                        AND NOT EXISTS (
                            SELECT 1 FROM verification_probe_waiters waiter
                            WHERE waiter.job_id=r.job_id
                                AND waiter.result_index=r.original_index
                                AND waiter.expires_at>?
                        ))
                ORDER BY j.is_cache_refresh ASC,
                    CASE WHEN j.retry_route='alternate_route' THEN 1 ELSE 0 END,
                    CASE WHEN j.execution_target=? THEN 0 ELSE 1 END,
                    COALESCE(turn.last_claimed_at, {epoch}), j.created_at
                LIMIT ?
            """, (
                execution_target,
                *claim_targets,
                _sql_ts(now),
                _sql_ts(now),
                execution_target,
                settings.scheduler_claim_scan_limit,
            )).fetchall()
            if not rows:
                connection.commit()
                return None
            has_cross_route_candidate = any(
                str(row[self._columns.index("retry_route")]) == "alternate_route"
                for row in rows
            )
            if postgres_active() and has_cross_route_candidate:
                cross_route_advisory = connection.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtext(?), hashtext(?))",
                    ("smtp-cross-route-claim", execution_target),
                ).fetchone()
                if not cross_route_advisory or not bool(cross_route_advisory[0]):
                    connection.rollback()
                    return None
            active_by_key: dict[str, int] = {}
            for key, slots in connection.execute("""
                SELECT mx_key, SUM(slots) FROM mx_scheduler_leases WHERE expires_at >= ? GROUP BY mx_key
            """, (_sql_ts(now),)):
                active_by_key[str(key)] = int(slots)
            active_cross_route = 0
            cross_route_by_key: dict[str, int] = {}
            if has_cross_route_candidate:
                active_cross_route = int(connection.execute("""
                    SELECT COUNT(DISTINCT lease.id) FROM job_leases lease
                    JOIN jobs job ON job.id=lease.job_id
                    WHERE lease.completed_at IS NULL AND lease.heartbeat_at>=?
                        AND job.retry_route='alternate_route'
                """, (_sql_ts(stale),)).fetchone()[0])
                cross_route_by_key = {
                    str(key): int(slots)
                    for key, slots in connection.execute("""
                    SELECT mx.mx_key, SUM(mx.slots) FROM mx_scheduler_leases mx
                    JOIN job_leases lease ON lease.id=mx.lease_id
                    JOIN jobs job ON job.id=lease.job_id
                    WHERE mx.expires_at>=? AND lease.completed_at IS NULL
                        AND job.retry_route='alternate_route'
                    GROUP BY mx.mx_key
                    """, (_sql_ts(now),))
                }
            job: Job | None = None
            owner_key: str | None = None
            indices: list[int] = []
            mx_slots: dict[str, int] = {}
            # A saturated provider must not leave an otherwise capable worker
            # idle. Scan a bounded fair queue and lease the first runnable work.
            for row in rows:
                candidate = self._job_from_row(row)
                candidate_is_prospecting = self._is_prospecting_job(connection, candidate.id)
                candidate_is_cross_route = candidate.retry_route == "alternate_route"
                if (
                    candidate_is_cross_route
                    and active_cross_route >= settings.smtp_cross_route_concurrency
                ):
                    continue
                leased: set[int] = set()
                for (raw_indices,) in connection.execute("""
                    SELECT indices_json FROM job_leases WHERE job_id=? AND completed_at IS NULL
                        AND heartbeat_at >= ?
                """, (candidate.id, _sql_ts(stale))):
                    try:
                        leased.update(int(index) for index in _json_load(raw_indices))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                candidate_indices: list[int] = []
                candidate_slots: dict[str, int] = {}
                candidate_load = dict(active_by_key)
                candidate_shard_size = (
                    1
                    if candidate_is_cross_route
                    else max(1, prospecting_shard_size)
                    if candidate_is_prospecting and prospecting_shard_size is not None
                    else max(1, shard_size)
                )
                # Resolve scheduler routes and profiles in batches. Per-email
                # lookups keep a PostgreSQL write transaction open for one
                # round trip per pending address.
                candidate_scan_limit = max(256, candidate_shard_size * 8)
                pending_rows = connection.execute("""
                    SELECT original_index, email FROM job_results WHERE job_id=?
                        AND progress_state='pending'
                        AND NOT EXISTS (
                            SELECT 1 FROM verification_probe_waiters waiter
                            WHERE waiter.job_id=job_results.job_id
                                AND waiter.result_index=job_results.original_index
                                AND waiter.expires_at>?
                        )
                    ORDER BY original_index LIMIT ?
                """, (candidate.id, _sql_ts(now), candidate_scan_limit)).fetchall()
                domains = {
                    self._scheduler_domain(str(email))
                    for _index, email in pending_rows
                    if self._scheduler_mx_key(str(email)) not in {"gmail", "microsoft", "qq"}
                }
                route_by_domain: dict[str, str] = {}
                if domains:
                    route_placeholders = ", ".join("?" for _ in domains)
                    route_by_domain = {
                        str(domain): str(scheduler_key)
                        for domain, scheduler_key in connection.execute(
                            f"SELECT domain, scheduler_key FROM scheduler_domain_routes "
                            f"WHERE domain IN ({route_placeholders})",
                            tuple(domains),
                        )
                    }
                pending_with_keys: list[tuple[int, str]] = []
                scheduler_keys: set[str] = set()
                for index, email in pending_rows:
                    email_text = str(email)
                    mx_key = self._scheduler_mx_key(email_text)
                    if mx_key not in {"gmail", "microsoft", "qq"}:
                        mx_key = route_by_domain.get(self._scheduler_domain(email_text), mx_key)
                    pending_with_keys.append((int(index), mx_key))
                    scheduler_keys.add(mx_key)
                profiles: dict[str, tuple[object, object, object]] = {}
                if scheduler_keys:
                    profile_placeholders = ", ".join("?" for _ in scheduler_keys)
                    profiles = {
                        str(key): (current_limit, pressure_events, cooldown_until)
                        for key, current_limit, pressure_events, cooldown_until in connection.execute(
                            "SELECT scheduler_key, current_limit, pressure_events, cooldown_until "
                            f"FROM scheduler_domain_profiles WHERE scheduler_key IN ({profile_placeholders})",
                            tuple(scheduler_keys),
                        )
                    }
                for index, mx_key in pending_with_keys:
                    profile = profiles.get(mx_key)
                    if (
                        index in leased
                        or (
                            not candidate_is_cross_route
                            and self._scheduler_profile_is_cooling_down_value(
                                profile[2] if profile else None, now
                            )
                        )
                        or (
                            candidate_is_cross_route
                            and cross_route_by_key.get(mx_key, 0)
                            >= settings.smtp_cross_route_per_mx_concurrency
                        )
                        or candidate_load.get(mx_key, 0) >= self._scheduler_profile_limit_from_row(
                            mx_key, profile, now,
                            prospecting=candidate_is_prospecting,
                        )
                    ):
                        continue
                    candidate_indices.append(int(index))
                    candidate_slots[mx_key] = candidate_slots.get(mx_key, 0) + 1
                    candidate_load[mx_key] = candidate_load.get(mx_key, 0) + 1
                    if len(candidate_indices) >= candidate_shard_size:
                        break
                if not candidate_indices:
                    continue
                job = candidate
                indices = candidate_indices
                mx_slots = candidate_slots
                owner_key = connection.execute("""
                    SELECT COALESCE(parent.owner_id, child.owner_id, child.id) FROM jobs child
                    LEFT JOIN jobs parent ON parent.id=child.parent_id WHERE child.id=?
                """, (job.id,)).fetchone()[0]
                break
            if job is None or owner_key is None:
                connection.commit()
                return None
            connection.execute("""
                INSERT INTO scheduler_owner_turns(target, owner_key, last_claimed_at) VALUES (?, ?, ?)
                ON CONFLICT(target, owner_key) DO UPDATE SET last_claimed_at=excluded.last_claimed_at
            """, (execution_target, owner_key, _sql_ts(now)))
            lease_id = uuid.uuid4().hex
            connection.execute("""
                INSERT INTO job_leases(id, job_id, worker_id, execution_target, indices_json, claimed_at, heartbeat_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (lease_id, job.id, worker_id, execution_target, json.dumps(indices), _sql_ts(now), _sql_ts(now)))
            placeholders = ", ".join("?" for _ in indices)
            connection.execute(
                f"UPDATE job_results SET progress_state='verifying' WHERE job_id=? "
                f"AND original_index IN ({placeholders}) AND progress_state='pending'",
                (job.id, *indices),
            )
            expires = _sql_ts(now + timedelta(seconds=settings.worker_lease_seconds))
            connection.executemany(
                "INSERT INTO mx_scheduler_leases(lease_id, mx_key, slots, expires_at) VALUES (?, ?, ?, ?)",
                [(lease_id, key, slots, expires) for key, slots in mx_slots.items()],
            )
            connection.execute("""
                UPDATE jobs SET status='running', worker_id=?, started_at=COALESCE(started_at, ?),
                    heartbeat_at=?, deferred_retry_at=NULL, error=NULL WHERE id=?
            """, (worker_id, _sql_ts(now), _sql_ts(now), job.id))
            connection.commit()
        job.status, job.worker_id, job.heartbeat_at = "running", worker_id, now
        job.started_at = job.started_at or now
        job.pending_indices, job.lease_id = indices, lease_id
        return job

    @staticmethod
    def _scheduler_mx_key(email: str) -> str:
        """Return a scheduler-wide provider/domain bucket, never a node-local key."""
        domain = email.rsplit("@", 1)[-1].lower()
        if domain in {"gmail.com", "googlemail.com"}:
            return "gmail"
        if domain in {"outlook.com", "hotmail.com", "live.com", "msn.com"}:
            return "microsoft"
        if domain in {"qq.com", "vip.qq.com", "foxmail.com"}:
            return "qq"
        return f"domain:{domain}"

    @staticmethod
    def _scheduler_domain(email: str) -> str:
        return email.rsplit("@", 1)[-1].strip().lower().rstrip(".")

    @staticmethod
    def _scheduler_key_for_mx_host(mx_host: str) -> str:
        host = mx_host.strip().lower().rstrip(".")
        if host.endswith((".google.com", ".googlemail.com")):
            return "gmail"
        if host.endswith(".protection.outlook.com"):
            return "microsoft"
        if host.endswith((".qq.com", ".foxmail.com")):
            return "qq"
        return f"mx:{host}"

    def _scheduler_key_for_email(self, connection: sqlite3.Connection, email: str) -> str:
        base_key = self._scheduler_mx_key(email)
        if base_key in {"gmail", "microsoft", "qq"}:
            return base_key
        domain = self._scheduler_domain(email)
        row = connection.execute(
            "SELECT scheduler_key FROM scheduler_domain_routes WHERE domain=?", (domain,)
        ).fetchone()
        return str(row[0]) if row else base_key

    def _scheduler_key_for_result(
        self, connection: sqlite3.Connection, email: str, result: dict[str, Any], now: datetime
    ) -> str:
        base_key = self._scheduler_mx_key(email)
        if base_key in {"gmail", "microsoft", "qq"}:
            return base_key
        mx_records = result.get("mx_records")
        if isinstance(mx_records, list):
            for raw_host in mx_records:
                host = str(raw_host).strip().lower().rstrip(".")
                if not host:
                    continue
                scheduler_key = self._scheduler_key_for_mx_host(host)
                connection.execute("""
                    INSERT INTO scheduler_domain_routes(domain, scheduler_key, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(domain) DO UPDATE SET
                        scheduler_key=excluded.scheduler_key, updated_at=excluded.updated_at
                """, (self._scheduler_domain(email), scheduler_key, _sql_ts(now)))
                return scheduler_key
        return self._scheduler_key_for_email(connection, email)

    @staticmethod
    def _scheduler_mx_capacity(mx_key: str) -> int:
        provider = mx_key.split(":", 1)[0]
        if provider == "gmail":
            return settings.scheduler_gmail_concurrency
        if provider == "microsoft":
            return settings.scheduler_microsoft_concurrency
        if provider == "qq":
            return settings.qq_worker_max_workers
        return settings.scheduler_default_domain_concurrency

    @classmethod
    def _scheduler_profile_bounds(cls, mx_key: str) -> tuple[int, int]:
        initial = cls._scheduler_mx_capacity(mx_key)
        if mx_key in {"gmail", "microsoft", "qq"}:
            return 1, initial
        return 1, max(initial, settings.scheduler_domain_max_concurrency)

    @staticmethod
    def _scheduler_successes_per_step(mx_key: str, *, prospecting: bool) -> int:
        if prospecting:
            return settings.prospecting_scheduler_successes_per_step
        if mx_key == "gmail":
            return settings.scheduler_gmail_successes_per_step
        if mx_key == "microsoft":
            return settings.scheduler_microsoft_successes_per_step
        return settings.scheduler_successes_per_step

    @classmethod
    def _ensure_scheduler_profile(
        cls, connection: sqlite3.Connection, mx_key: str, now: datetime
    ) -> tuple[int, int, int, str | None]:
        row = connection.execute("""
            SELECT current_limit, success_streak, pressure_events, cooldown_until
            FROM scheduler_domain_profiles WHERE scheduler_key=?
        """, (mx_key,)).fetchone()
        if row is not None:
            return int(row[0]), int(row[1]), int(row[2]), str(row[3]) if row[3] else None
        minimum, maximum = cls._scheduler_profile_bounds(mx_key)
        initial = max(minimum, min(cls._scheduler_mx_capacity(mx_key), maximum))
        connection.execute("""
            INSERT INTO scheduler_domain_profiles(
                scheduler_key, current_limit, success_streak, successes, pressure_events, last_seen_at
            ) VALUES (?, ?, 0, 0, 0, ?)
        """, (mx_key, initial, _sql_ts(now)))
        return initial, 0, 0, None

    @classmethod
    def _scheduler_profile_limit(
        cls, connection: sqlite3.Connection, mx_key: str, now: datetime, *, prospecting: bool = False,
    ) -> int:
        row = connection.execute(
            "SELECT current_limit, pressure_events, cooldown_until FROM scheduler_domain_profiles "
            "WHERE scheduler_key=?", (mx_key,)
        ).fetchone()
        return cls._scheduler_profile_limit_from_row(
            mx_key, row, now, prospecting=prospecting
        )

    @classmethod
    def _scheduler_profile_limit_from_row(
        cls,
        mx_key: str,
        row: tuple[object, object, object] | None,
        now: datetime,
        *,
        prospecting: bool = False,
    ) -> int:
        _minimum, maximum = cls._scheduler_profile_bounds(mx_key)
        if row is None:
            current = (
                maximum
                if mx_key in {"gmail", "microsoft", "qq"}
                else cls._scheduler_mx_capacity(mx_key)
            )
            pressure_events = 0
            cooldown_until = None
        else:
            current = int(row[0])
            pressure_events = int(row[1])
            cooldown_until = str(row[2]) if row[2] else None
        limit = max(1, min(current, maximum))
        if not prospecting or mx_key in {"gmail", "microsoft", "qq"} or pressure_events:
            return limit
        if cooldown_until:
            try:
                cooldown_dt = _dt(cooldown_until)
                if cooldown_dt is not None and cooldown_dt > now:
                    return limit
            except ValueError:
                return limit
        return max(
            limit,
            min(maximum, settings.prospecting_scheduler_initial_domain_concurrency),
        )

    @staticmethod
    def _is_prospecting_job(connection: sqlite3.Connection, job_id: str) -> bool:
        """Keep discovery-only scheduling separate from ordinary verification."""
        try:
            return connection.execute(
                "SELECT 1 FROM prospecting_runs WHERE verification_job_id=?", (job_id,)
            ).fetchone() is not None
        except Exception:
            # Minimal installs and scheduler-only tests do not create the
            # prospecting tables, so they retain the normal verification path.
            return False

    @staticmethod
    def _scheduler_profile_is_cooling_down(
        connection: sqlite3.Connection, mx_key: str, now: datetime
    ) -> bool:
        """Do not send a fresh probe while the receiver cooldown is active."""
        row = connection.execute(
            "SELECT cooldown_until FROM scheduler_domain_profiles WHERE scheduler_key=?", (mx_key,)
        ).fetchone()
        return JobStore._scheduler_profile_is_cooling_down_value(row[0] if row else None, now)

    @staticmethod
    def _scheduler_profile_is_cooling_down_value(cooldown_until: object, now: datetime) -> bool:
        if not cooldown_until:
            return False
        try:
            parsed = _dt(cooldown_until)
            return parsed is not None and parsed > now
        except ValueError:
            return False

    @staticmethod
    def _scheduler_pressure_signal(result: dict[str, Any]) -> bool:
        """Only receiver pressure, never invalid recipients, reduces concurrency."""
        if is_recipient_mailbox_full(result):
            return False
        if smtp_temporary_status(result):
            return True
        detail = " ".join(
            str(result.get(field) or "")
            for field in ("smtp_raw_result", "smtp_result", "message")
        ).lower()
        return any(marker in detail for marker in (
            "timeout", "timed out", "connection reset", "connection refused",
            "connection failed", "server disconnected", "too many connections",
            "rate limit", "rate limited", "throttl",
        ))

    @staticmethod
    def _scheduler_receiver_cooldown_seconds(result: dict[str, Any]) -> int:
        """Use the retry classifier's receiver cooldown when it is available."""
        try:
            requested = int(result.get("receiver_cooldown_seconds") or 0)
        except (TypeError, ValueError):
            requested = 0
        return max(settings.scheduler_cooldown_seconds, min(requested, 6 * 60 * 60))

    @staticmethod
    def _scheduler_success_signal(mx_key: str, result: dict[str, Any]) -> bool:
        checks = result.get("checks")
        if isinstance(checks, dict) and checks.get("smtp") in {True, False}:
            return True
        # Outlook-family addresses use the Microsoft HTTPS check instead of SMTP.
        return mx_key == "microsoft" and result.get("deliverable") in {True, False}

    def _record_scheduler_outcomes(
        self, connection: sqlite3.Connection, job_id: str, indices: list[int], now: datetime
    ) -> None:
        if not indices:
            return
        placeholders = ", ".join("?" for _ in indices)
        prospecting = self._is_prospecting_job(connection, job_id)
        grouped: dict[str, dict[str, int]] = {}
        for email, raw_result in connection.execute(
            f"SELECT email, result_json FROM job_results WHERE job_id=? AND original_index IN ({placeholders})",
            (job_id, *indices),
        ):
            try:
                result = _json_load(raw_result)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(result, dict):
                continue
            mx_key = self._scheduler_key_for_result(connection, str(email), result, now)
            outcome = grouped.setdefault(
                mx_key, {"success": 0, "pressure": 0, "cooldown_seconds": 0}
            )
            if self._scheduler_pressure_signal(result):
                outcome["pressure"] += 1
                outcome["cooldown_seconds"] = max(
                    outcome["cooldown_seconds"],
                    self._scheduler_receiver_cooldown_seconds(result),
                )
            elif self._scheduler_success_signal(mx_key, result):
                outcome["success"] += 1

        for mx_key, outcome in grouped.items():
            current, streak, pressure_events, cooldown_until = self._ensure_scheduler_profile(
                connection, mx_key, now
            )
            minimum, maximum = self._scheduler_profile_bounds(mx_key)
            current = max(minimum, min(current, maximum))
            cooldown_active = False
            if cooldown_until:
                try:
                    cd = _dt(cooldown_until)
                    cooldown_active = cd is not None and cd > now
                except ValueError:
                    cooldown_active = False
            adjusted_at: str | None = None
            next_cooldown: str | None = cooldown_until
            if outcome["pressure"]:
                streak = 0
                pressure_events += outcome["pressure"]
                if not cooldown_active:
                    current = max(minimum, (current + 1) // 2)
                    next_cooldown = _sql_ts(
                        now + timedelta(seconds=outcome["cooldown_seconds"])
                    )
                    adjusted_at = _sql_ts(now)
            elif outcome["success"]:
                if cooldown_active:
                    streak = 0
                else:
                    streak += outcome["success"]
                    successes_per_step = self._scheduler_successes_per_step(
                        mx_key, prospecting=prospecting
                    )
                    step_size = settings.prospecting_scheduler_step_size if prospecting else 1
                    if streak >= successes_per_step and current < maximum:
                        current = min(maximum, current + step_size)
                        streak = 0
                        adjusted_at = _sql_ts(now)
            connection.execute("""
                UPDATE scheduler_domain_profiles SET current_limit=?, success_streak=?,
                    successes=successes+?, pressure_events=?, last_seen_at=?,
                    last_adjusted_at=COALESCE(?, last_adjusted_at), cooldown_until=?
                WHERE scheduler_key=?
            """, (
                current,
                streak,
                outcome["success"],
                pressure_events,
                _sql_ts(now),
                adjusted_at,
                next_cooldown,
                mx_key,
            ))

    def lease_valid(
        self, job_id: str, worker_id: str, lease_id: str, execution_target: str | None = None,
    ) -> bool:
        stale = _sql_ts(utc_now() - timedelta(seconds=settings.worker_lease_seconds))
        target_clause = " AND execution_target=?" if execution_target else ""
        parameters: tuple[Any, ...] = (lease_id, job_id, worker_id, stale)
        if execution_target:
            parameters += (execution_target,)
        with closing(self._connect()) as connection:
            return connection.execute(f"""
                SELECT 1 FROM job_leases WHERE id=? AND job_id=? AND worker_id=?
                    AND completed_at IS NULL AND heartbeat_at >= ?
                    {target_clause}
            """, parameters).fetchone() is not None

    def lease_accepts_results(
        self, job_id: str, worker_id: str, lease_id: str, indices: list[int]
    ) -> bool:
        """A lease may only report the exact index set granted to it."""
        if not self.lease_valid(job_id, worker_id, lease_id):
            return False
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT indices_json FROM job_leases WHERE id=? AND job_id=? AND worker_id=?",
                (lease_id, job_id, worker_id),
            ).fetchone()
        if row is None:
            return False
        try:
            granted = {int(index) for index in _json_load(row[0])}
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        return all(index in granted for index in indices)

    def heartbeat_lease(self, job_id: str, worker_id: str, lease_id: str) -> bool:
        now = utc_now()
        stale = _sql_ts(now - timedelta(seconds=settings.worker_lease_seconds))
        with closing(self._connect()) as connection:
            changed = connection.execute("""
                UPDATE job_leases SET heartbeat_at=? WHERE id=? AND job_id=? AND worker_id=?
                    AND completed_at IS NULL AND heartbeat_at >= ?
            """, (_sql_ts(now), lease_id, job_id, worker_id, stale)).rowcount
            if changed:
                connection.execute(
                    "UPDATE mx_scheduler_leases SET expires_at=? WHERE lease_id=?",
                    (_sql_ts(now + timedelta(seconds=settings.worker_lease_seconds)), lease_id),
                )
                connection.execute("UPDATE jobs SET heartbeat_at=? WHERE id=?", (_sql_ts(now), job_id))
                self._renew_probe_leases_in_connection(connection, job_id, now)
        return bool(changed)

    def report_lease_results(
        self,
        job_id: str,
        worker_id: str,
        lease_id: str,
        results: list[dict[str, Any]],
        *,
        execution_target: str | None = None,
    ) -> bool:
        """Atomically accept a shard callback and renew its still-active lease."""
        self.initialize()
        now = utc_now()
        stale = _sql_ts(now - timedelta(seconds=settings.worker_lease_seconds))
        target_clause = " AND execution_target=?" if execution_target else ""
        parameters: tuple[Any, ...] = (lease_id, job_id, worker_id, stale)
        if execution_target:
            parameters += (execution_target,)
        with self._lock, closing(self._connect()) as connection:
            begin_immediate(connection)
            row = connection.execute(f"""
                SELECT indices_json FROM job_leases WHERE id=? AND job_id=? AND worker_id=?
                    AND completed_at IS NULL AND heartbeat_at >= ?{target_clause}
            """, parameters).fetchone()
            if row is None:
                connection.rollback()
                return False
            try:
                granted = {int(index) for index in _json_load(row[0])}
                reported = {int(result["original_index"]) for result in results}
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                connection.rollback()
                return False
            if not reported.issubset(granted):
                connection.rollback()
                return False
            self._upsert_results(connection, job_id, results)
            connection.execute(
                "UPDATE job_leases SET heartbeat_at=? WHERE id=?",
                (_sql_ts(now), lease_id),
            )
            connection.execute(
                "UPDATE mx_scheduler_leases SET expires_at=? WHERE lease_id=?",
                (_sql_ts(now + timedelta(seconds=settings.worker_lease_seconds)), lease_id),
            )
            connection.execute("UPDATE jobs SET heartbeat_at=? WHERE id=?", (_sql_ts(now), job_id))
            self._renew_probe_leases_in_connection(connection, job_id, now)
            connection.commit()
        return True

    def complete_lease(self, job_id: str, worker_id: str, lease_id: str) -> bool:
        self.initialize()
        now = utc_now()
        stale = _sql_ts(now - timedelta(seconds=settings.worker_lease_seconds))
        with self._lock, closing(self._connect()) as connection:
            begin_immediate(connection)
            row = connection.execute("""
                SELECT indices_json FROM job_leases WHERE id=? AND job_id=? AND worker_id=?
                    AND completed_at IS NULL AND heartbeat_at >= ?
            """, (lease_id, job_id, worker_id, stale)).fetchone()
            if row is None:
                connection.rollback()
                return False
            try:
                indices = [int(index) for index in _json_load(row[0])]
            except (TypeError, ValueError, json.JSONDecodeError):
                indices = []
            changed = connection.execute("""
                UPDATE job_leases SET completed_at=? WHERE id=? AND job_id=? AND worker_id=?
                    AND completed_at IS NULL
            """, (_sql_ts(now), lease_id, job_id, worker_id)).rowcount
            connection.execute("DELETE FROM mx_scheduler_leases WHERE lease_id=?", (lease_id,))
            if changed:
                self._record_scheduler_outcomes(connection, job_id, indices, now)
            connection.commit()
        return bool(changed)

    def complete_lease_with_results(
        self,
        job_id: str,
        worker_id: str,
        lease_id: str,
        results: list[dict[str, Any]],
        *,
        execution_target: str | None = None,
    ) -> bool:
        """Atomically persist a final shard, reconcile conflicts, and close its lease."""
        self.initialize()
        now = utc_now()
        stale = _sql_ts(now - timedelta(seconds=settings.worker_lease_seconds))
        target_clause = " AND execution_target=?" if execution_target else ""
        parameters: tuple[Any, ...] = (lease_id, job_id, worker_id, stale)
        if execution_target:
            parameters += (execution_target,)
        with self._lock, closing(self._connect()) as connection:
            begin_immediate(connection)
            row = connection.execute(f"""
                SELECT indices_json FROM job_leases WHERE id=? AND job_id=? AND worker_id=?
                    AND completed_at IS NULL AND heartbeat_at >= ?{target_clause}
            """, parameters).fetchone()
            if row is None:
                connection.rollback()
                return False
            try:
                indices = [int(index) for index in _json_load(row[0])]
                reported = {int(result["original_index"]) for result in results}
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                connection.rollback()
                return False
            if not reported.issubset(set(indices)):
                connection.rollback()
                return False
            self._upsert_results(connection, job_id, results)
            self._reconcile_catch_all_conflicts_in_connection(connection, job_id)
            changed = connection.execute("""
                UPDATE job_leases SET completed_at=? WHERE id=? AND job_id=? AND worker_id=?
                    AND completed_at IS NULL
            """, (_sql_ts(now), lease_id, job_id, worker_id)).rowcount
            connection.execute("DELETE FROM mx_scheduler_leases WHERE lease_id=?", (lease_id,))
            if changed:
                self._record_scheduler_outcomes(connection, job_id, indices, now)
            connection.commit()
        return bool(changed)

    def abandon_lease(self, job_id: str, worker_id: str, lease_id: str) -> bool:
        """Return only a failed worker's unfinished shard to the queue."""
        self.initialize()
        stale = _sql_ts(utc_now() - timedelta(seconds=settings.worker_lease_seconds))
        with self._lock, closing(self._connect()) as connection:
            begin_immediate(connection)
            row = connection.execute("""
                SELECT indices_json FROM job_leases
                WHERE id=? AND job_id=? AND worker_id=? AND completed_at IS NULL AND heartbeat_at >= ?
            """, (lease_id, job_id, worker_id, stale)).fetchone()
            if row is None:
                connection.rollback()
                return False
            try:
                indices = [int(index) for index in (_json_load(row[0]) or [])]
            except (TypeError, ValueError, json.JSONDecodeError):
                indices = []
            if indices:
                placeholders = ", ".join("?" for _ in indices)
                connection.execute(
                    f"UPDATE job_results SET progress_state='pending' WHERE job_id=? "
                    f"AND original_index IN ({placeholders}) AND progress_state='verifying'",
                    (job_id, *indices),
                )
            now = _sql_ts(utc_now())
            connection.execute("UPDATE job_leases SET completed_at=? WHERE id=?", (now, lease_id))
            connection.execute("DELETE FROM mx_scheduler_leases WHERE lease_id=?", (lease_id,))
            connection.execute(
                "UPDATE jobs SET status='queued', worker_id=NULL, heartbeat_at=NULL WHERE id=? AND status='running'",
                (job_id,),
            )
            connection.commit()
        return True

    def requeue_orphaned_results(self) -> int:
        """Release only rows whose worker lease is no longer active."""
        self.initialize()
        stale = _sql_ts(utc_now() - timedelta(seconds=settings.worker_lease_seconds))
        with self._lock, closing(self._connect()) as connection:
            begin_immediate(connection)
            released = self._release_orphaned_results(connection, stale)
            connection.commit()
            return released

    @staticmethod
    def _release_orphaned_results(connection: sqlite3.Connection, stale_before: Any) -> int:
        """Return verifying rows to the queue when no fresh lease owns them."""
        return connection.execute("""
            UPDATE job_results SET progress_state='pending'
            WHERE progress_state='verifying'
              AND NOT EXISTS (
                  SELECT 1 FROM job_leases lease
                  WHERE lease.job_id=job_results.job_id
                    AND lease.completed_at IS NULL AND lease.heartbeat_at >= ?
              )
        """, (stale_before,)).rowcount

    def pending_count(self, job_id: str) -> int:
        with closing(self._connect()) as connection:
            return int(connection.execute("""
                SELECT COUNT(*) FROM job_results WHERE job_id=? AND progress_state IN ('pending', 'verifying')
            """, (job_id,)).fetchone()[0])

    def heartbeat(self, job: Job) -> None:
        job.heartbeat_at = utc_now()
        with closing(self._connect()) as connection:
            changed = connection.execute(
                "UPDATE jobs SET heartbeat_at = ? WHERE id = ? AND worker_id = ? AND status = 'running'",
                (_sql_ts(job.heartbeat_at), job.id, job.worker_id),
            ).rowcount
            if changed:
                self._renew_probe_leases_in_connection(connection, job.id, job.heartbeat_at)

    def requeue_stale_jobs(self) -> int:
        """Return expired leases to their original execution-target queue."""
        self.initialize()
        stale_before = utc_now() - timedelta(seconds=settings.worker_lease_seconds)
        with closing(self._connect()) as connection:
            begin_immediate(connection)
            stale_before_bind = _sql_ts(stale_before)
            self._release_orphaned_results(connection, stale_before_bind)
            requeued = connection.execute(
                """
                UPDATE jobs SET status='queued', worker_id=NULL, heartbeat_at=NULL,
                    error='工作节点已重新领取任务'
                WHERE status='running' AND heartbeat_at IS NOT NULL AND heartbeat_at < ?
                """,
                (stale_before_bind,),
            ).rowcount
            connection.commit()
            return requeued

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
                emails = _json_load(emails_json)
                results = _json_load(results_json)
                failed_results = []
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
                    failed_results.append(result)
                connection.execute(
                    "UPDATE jobs SET results_json=? WHERE id=?",
                    (json.dumps(results, ensure_ascii=False), job_id),
                )
                self._upsert_results(connection, str(job_id), failed_results)
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
            last_seen_at=_dt(row[1]) if row[1] else None,
            wake_requested_at=_dt(row[2]) if row[2] else None,
            wake_deadline_at=_dt(row[3]) if row[3] else None,
            wake_attempts=int(row[4]),
            last_wake_error=row[5],
            idle_since=_dt(row[6]) if row[6] else None,
            stop_requested_at=_dt(row[7]) if row[7] else None,
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

    def record_worker_seen(
        self, target: str, worker_id: str, capacity: int | None = None
    ) -> None:
        """Record a remote heartbeat in the canonical node registry."""
        self.initialize()
        now = _sql_ts(utc_now())
        with closing(self._connect()) as connection:
            existing = connection.execute(
                "SELECT capacity FROM worker_nodes WHERE target=? AND worker_id=?",
                (target, worker_id),
            ).fetchone()
            node_capacity = max(1, capacity if capacity is not None else (
                int(existing[0]) if existing else 1
            ))
            connection.execute(
                """INSERT INTO worker_nodes(target, worker_id, capacity, health, last_seen_at)
                VALUES (?, ?, ?, 'healthy', ?)
                ON CONFLICT(target, worker_id) DO UPDATE SET
                    capacity=excluded.capacity, health='healthy', last_seen_at=excluded.last_seen_at""",
                (target, worker_id, node_capacity, now),
            )
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

    def mark_worker_offline(self, target: str, worker_id: str) -> None:
        """Immediately retire a node deliberately stopped by its lifecycle."""
        self.initialize()
        with closing(self._connect()) as connection:
            connection.execute(
                """UPDATE worker_nodes SET health='offline'
                   WHERE target=? AND worker_id=?""",
                (target, worker_id),
            )
            connection.commit()

    def reconcile_worker_nodes(self, now: datetime | None = None) -> dict[str, int]:
        """Derive stale/offline node state from durable heartbeats."""
        self.initialize()
        now = now or utc_now()
        stale_before = _sql_ts(now - timedelta(seconds=settings.node_stale_seconds))
        offline_seconds = max(settings.node_stale_seconds + 1, settings.node_offline_seconds)
        offline_before = _sql_ts(now - timedelta(seconds=offline_seconds))
        with self._lock, closing(self._connect()) as connection:
            begin_immediate(connection)
            offline = connection.execute(
                "UPDATE worker_nodes SET health='offline' WHERE health!='offline' AND last_seen_at < ?",
                (offline_before,),
            ).rowcount
            stale = connection.execute(
                "UPDATE worker_nodes SET health='stale' WHERE health='healthy' AND last_seen_at < ?",
                (stale_before,),
            ).rowcount
            connection.commit()
        return {"stale": int(stale), "offline": int(offline)}

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
        result: dict[str, datetime] = {}
        for worker_id, last_seen_at in rows:
            parsed = _dt(last_seen_at)
            if parsed is not None:
                result[str(worker_id)] = parsed
        return result

    def record_wake_attempt(
        self, target: str, deadline: datetime | None, error: str | None
    ) -> WorkerRuntime:
        self.initialize()
        now = _sql_ts(utc_now())
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
                (target, now, _sql_ts(deadline) if deadline else None, error),
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
        now = _sql_ts(utc_now())
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
                (target, _sql_ts(utc_now()), error),
            )

    def is_stopped(self, job_id: str) -> bool:
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        return row is not None and row[0] == "stopped"

    def reroute_queued_jobs(self, source_target: str, destination_target: str, message: str) -> int:
        """Move unclaimed remote work to an available execution target."""
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            begin_immediate(connection)
            try:
                cursor = connection.execute(
                    """
                    UPDATE jobs SET execution_target=?, worker_id=NULL, heartbeat_at=NULL,
                        deferred_retry_at=NULL, error=?
                    WHERE execution_target=? AND status='queued'
                    """,
                    (destination_target, message, source_target),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return int(cursor.rowcount)

    def reroute_stale_queued_jobs(
        self, source_target: str, destination_target: str, older_than_seconds: int, message: str,
    ) -> int:
        """Fall back only work that a remote target has not claimed in time."""
        self.initialize()
        cutoff = utc_now() - timedelta(seconds=max(1, older_than_seconds))
        with self._lock, closing(self._connect()) as connection:
            begin_immediate(connection)
            try:
                cursor = connection.execute(
                    """
                    UPDATE jobs SET execution_target=?, worker_id=NULL, heartbeat_at=NULL,
                        deferred_retry_at=NULL, error=?
                    WHERE execution_target=? AND status='queued' AND created_at <= ?
                    """,
                    (destination_target, message, source_target, _sql_ts(cutoff)),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return int(cursor.rowcount)

    def stop(self, job_id: str) -> Job | None:
        """Stop a queued or running job without discarding completed results."""
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            begin_immediate(connection)
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

    def stop_with_reason(self, job_id: str, reason: str) -> Job | None:
        """Stop a job because a receiver explicitly rejected further probing."""
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            begin_immediate(connection)
            row = connection.execute(
                f"SELECT {self._select_columns()} FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            job = self._job_from_row(row)
            if job.status not in {"queued", "running"}:
                connection.commit()
                return self._hydrate_results(job)
            now = utc_now().isoformat()
            connection.execute(
                """UPDATE jobs SET status='stopped', finished_at=?, error=?,
                    worker_id=NULL, heartbeat_at=NULL WHERE id=?""",
                (now, reason[:500], job_id),
            )
            lease_ids = [item[0] for item in connection.execute(
                "SELECT id FROM job_leases WHERE job_id=? AND completed_at IS NULL", (job_id,)
            )]
            connection.execute(
                "UPDATE job_leases SET completed_at=? WHERE job_id=? AND completed_at IS NULL",
                (now, job_id),
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

    def defer_job(self, job_id: str, until: datetime, reason: str) -> Job | None:
        """Release pending work until a receiver's cooling period ends."""
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            begin_immediate(connection)
            changed = connection.execute(
                """UPDATE jobs SET status='queued', worker_id=NULL, heartbeat_at=NULL,
                    deferred_retry_at=?, error=? WHERE id=? AND status IN ('queued', 'running')""",
                (until.isoformat(), reason[:500], job_id),
            ).rowcount
            if not changed:
                connection.rollback()
                return None
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
            connection.commit()
        return self.get(job_id)

    def resume(self, job_id: str) -> tuple[Job | None, list[Job]]:
        """Resume a stopped task in place and return work that was re-queued."""
        self.initialize()
        with self._lock, closing(self._connect()) as connection:
            begin_immediate(connection)
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

    def _record_cache_metrics(self, _connection=None, **counters: int) -> None:
        """Aggregate telemetry off the request path and flush it in one small write."""
        values = {
            name: max(0, int(counters.get(name, 0))) for name in _CACHE_METRIC_NAMES
        }
        if not any(values.values()):
            return
        day = utc_now().date().isoformat()
        global _CACHE_METRICS_FLUSHING
        with _CACHE_METRICS_LOCK:
            pending = _CACHE_METRICS_PENDING.setdefault(
                day, {name: 0 for name in _CACHE_METRIC_NAMES},
            )
            for name, value in values.items():
                pending[name] += value
            if _CACHE_METRICS_FLUSHING:
                return
            _CACHE_METRICS_FLUSHING = True
        threading.Thread(
            target=self._cache_metrics_flush_loop,
            name="verification-cache-metrics",
            daemon=True,
        ).start()

    def _cache_metrics_flush_loop(self) -> None:
        global _CACHE_METRICS_FLUSHING
        while True:
            time.sleep(settings.verification_cache_metrics_flush_seconds)
            self.flush_cache_metrics()
            with _CACHE_METRICS_LOCK:
                if _CACHE_METRICS_PENDING:
                    continue
                _CACHE_METRICS_FLUSHING = False
                return

    def flush_cache_metrics(self) -> bool:
        """Persist buffered counters; failures are retried without blocking verification."""
        with _CACHE_METRICS_LOCK:
            snapshots = {
                day: dict(values) for day, values in _CACHE_METRICS_PENDING.items()
            }
            _CACHE_METRICS_PENDING.clear()
        if not snapshots:
            return True
        try:
            with closing(self._connect()) as connection:
                for day, values in snapshots.items():
                    day_value = datetime.fromisoformat(day).date() if postgres_active() else day
                    connection.execute(f"""
                        INSERT INTO verification_cache_days(
                            day, {', '.join(_CACHE_METRIC_NAMES)}, updated_at
                        ) VALUES (?, {', '.join('?' for _ in _CACHE_METRIC_NAMES)}, ?)
                        ON CONFLICT(day) DO UPDATE SET
                            {', '.join(
                                f'{name}=verification_cache_days.{name}+excluded.{name}'
                                for name in _CACHE_METRIC_NAMES
                            )},
                            updated_at=excluded.updated_at
                    """, (
                        day_value, *(values[name] for name in _CACHE_METRIC_NAMES),
                        _sql_ts(utc_now()),
                    ))
        except Exception:  # noqa: BLE001 - telemetry must not fail verification
            with _CACHE_METRICS_LOCK:
                for day, values in snapshots.items():
                    pending = _CACHE_METRICS_PENDING.setdefault(
                        day, {name: 0 for name in _CACHE_METRIC_NAMES},
                    )
                    for name, value in values.items():
                        pending[name] += value
            return False
        return True

    @staticmethod
    def _cache_policy_kwargs() -> dict[str, int]:
        return {
            "deliverable_first_days": settings.verification_cache_deliverable_first_days,
            "deliverable_repeat_days": settings.verification_cache_deliverable_repeat_days,
            "deliverable_stable_days": settings.verification_cache_deliverable_stable_days,
            "permanent_days": settings.verification_cache_permanent_days,
            "mailbox_full_hours": settings.verification_cache_mailbox_full_hours,
            "stale_days": settings.verification_cache_stale_days,
        }

    def cached_results(
        self, emails: list[str], *, claim_refresh: bool = False,
    ) -> dict[str, dict[str, Any]]:
        self.initialize()
        now = utc_now()
        keys = list(dict.fromkeys(
            str(email).strip().lower() for email in emails if str(email).strip()
        ))
        found: dict[str, dict[str, Any]] = {}
        stale_seen = 0
        refresh_candidates: list[tuple[datetime, str]] = []
        with closing(self._connect()) as connection:
            for start in range(0, len(keys), 800):
                batch = keys[start : start + 800]
                placeholders = ", ".join("?" for _ in batch)
                rows = connection.execute(f"""
                    SELECT email, result_json, expires_at, updated_at, outcome_class,
                        verified_at, stale_expires_at, hit_count, refresh_requested_at
                    FROM verification_cache WHERE email IN ({placeholders})
                        AND outcome_class IN ('deliverable', 'permanent_invalid', 'mailbox_full')
                """, tuple(batch)).fetchall()
                fresh_keys: list[str] = []
                for row in rows:
                    email = str(row[0]).lower()
                    expires_at = _dt(row[2])
                    stale_expires_at = _dt(row[6])
                    if expires_at is None or expires_at <= now:
                        if stale_expires_at is not None and stale_expires_at > now:
                            stale_seen += 1
                        continue
                    result = dict(_json_load(row[1], default={}) or {})
                    verified_at = _dt(row[5]) or _dt(row[3]) or now
                    result.update({
                        "cache_hit": True,
                        "cache_age_seconds": max(0, round((now - verified_at).total_seconds())),
                        "cache_outcome": str(row[4] or "legacy"),
                        "progress_state": "completed",
                    })
                    found[email] = result
                    fresh_keys.append(email)
                    if (
                        claim_refresh
                        and str(row[4] or "") == "deliverable"
                        and expires_at <= now + timedelta(
                            hours=settings.verification_cache_refresh_ahead_hours
                        )
                        and int(row[7] or 0) + 1 >= settings.verification_cache_refresh_min_hits
                    ):
                        requested_at = _dt(row[8])
                        if requested_at is None or requested_at <= now - timedelta(
                            hours=settings.verification_cache_refresh_cooldown_hours
                        ):
                            refresh_candidates.append((expires_at, email))
                if fresh_keys:
                    fresh_placeholders = ", ".join("?" for _ in fresh_keys)
                    connection.execute(f"""
                        UPDATE verification_cache
                        SET hit_count=hit_count+1, last_hit_at=?
                        WHERE email IN ({fresh_placeholders})
                    """, (_sql_ts(now), *fresh_keys))

            refresh_due: set[str] = set()
            refresh_limit = settings.verification_cache_refresh_max_per_request
            for _expires_at, email in sorted(refresh_candidates)[:refresh_limit]:
                changed = connection.execute("""
                    UPDATE verification_cache SET refresh_requested_at=?
                    WHERE email=? AND expires_at>? AND outcome_class='deliverable'
                        AND hit_count>=?
                        AND (refresh_requested_at IS NULL OR refresh_requested_at<=?)
                """, (
                    _sql_ts(now), email, _sql_ts(now),
                    settings.verification_cache_refresh_min_hits,
                    _sql_ts(now - timedelta(
                        hours=settings.verification_cache_refresh_cooldown_hours
                    )),
                )).rowcount
                if changed:
                    refresh_due.add(email)
            for email in refresh_due:
                found[email]["_cache_refresh_due"] = True
            self._record_cache_metrics(
                connection,
                lookups=len(keys), fresh_hits=len(found), misses=max(0, len(keys) - len(found)),
                stale_seen=stale_seen, refresh_scheduled=len(refresh_due),
            )
        return found

    def cache_results(
        self, results: list[dict[str, Any]], *, owner_job_id: str | None = None,
    ) -> list[str]:
        self.initialize()
        now = utc_now()
        by_email = {
            str(result.get("email") or "").strip().lower(): dict(result)
            for result in results
            if str(result.get("email") or "").strip() and not result.get("cache_hit")
        }
        deliverable_keys = [
            email for email, result in by_email.items()
            if result.get("deliverable") is True and not is_cache_excluded(result)
        ]
        history: dict[str, tuple[datetime, datetime, int]] = {}
        with closing(self._connect()) as connection:
            for start in range(0, len(deliverable_keys), 800):
                batch = deliverable_keys[start : start + 800]
                placeholders = ", ".join("?" for _ in batch)
                for email, first, last, count in connection.execute(f"""
                    SELECT email, first_confirmed_at, last_confirmed_at, confirmation_count
                    FROM verified_emails WHERE email IN ({placeholders})
                """, tuple(batch)).fetchall():
                    history[str(email)] = (_dt(first) or now, _dt(last) or now, int(count or 1))

        cache_rows = []
        verified_rows = []
        decisions: dict[str, str] = {}
        payloads: dict[str, dict[str, Any]] = {}
        for email, raw_result in by_email.items():
            payload = sanitize_cached_result(raw_result)
            prior = history.get(email)
            first = prior[0] if prior else now
            last = prior[1] if prior else None
            confirmation_count = prior[2] if prior else 0
            if raw_result.get("deliverable") is True and not is_cache_excluded(raw_result):
                if last is None or now - last >= timedelta(hours=1):
                    confirmation_count += 1
                verified_rows.append((
                    email, _sql_ts(first), _sql_ts(now),
                    json.dumps(payload, ensure_ascii=False, default=str), confirmation_count,
                ))
            decision = cache_decision(
                raw_result,
                confirmation_count=confirmation_count,
                first_confirmed_at=first,
                now=now,
                **self._cache_policy_kwargs(),
            )
            if decision is None:
                continue
            decisions[email] = decision.outcome_class
            payloads[email] = payload
            cache_rows.append((
                email, json.dumps(payload, ensure_ascii=False, default=str),
                _sql_ts(now + decision.fresh_for), _sql_ts(now), decision.outcome_class,
                _sql_ts(now), _sql_ts(now + decision.stale_for),
            ))

        affected_jobs: set[str] = set()
        with closing(self._connect()) as connection:
            if verified_rows:
                maximum = "GREATEST" if postgres_active() else "MAX"
                connection.executemany(f"""
                    INSERT INTO verified_emails(
                        email, first_confirmed_at, last_confirmed_at, result_json,
                        confirmation_count
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(email) DO UPDATE SET
                        last_confirmed_at=excluded.last_confirmed_at,
                        result_json=excluded.result_json,
                        confirmation_count={maximum}(
                            verified_emails.confirmation_count, excluded.confirmation_count
                        )
                """, verified_rows)
            if cache_rows:
                connection.executemany("""
                    INSERT INTO verification_cache(
                        email, result_json, expires_at, updated_at, outcome_class,
                        verified_at, stale_expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(email) DO UPDATE SET
                        result_json=excluded.result_json, expires_at=excluded.expires_at,
                        updated_at=excluded.updated_at, outcome_class=excluded.outcome_class,
                        verified_at=excluded.verified_at,
                        stale_expires_at=excluded.stale_expires_at,
                        refresh_requested_at=NULL
                """, cache_rows)
                for email, payload in payloads.items():
                    if owner_job_id is None:
                        continue
                    owns_probe = connection.execute("""
                        SELECT 1 FROM verification_probe_leases
                        WHERE email=? AND owner_job_id=? AND expires_at>?
                    """, (email, owner_job_id, _sql_ts(now))).fetchone()
                    if owns_probe is None:
                        continue
                    waiters = connection.execute("""
                        SELECT job_id, result_index FROM verification_probe_waiters
                        WHERE email=? AND owner_job_id=? AND expires_at>?
                    """, (email, owner_job_id, _sql_ts(now))).fetchall()
                    for job_id, result_index in waiters:
                        shared = dict(payload)
                        shared.update({
                            "email": email, "original_index": int(result_index),
                            "progress_state": "completed", "cache_hit": True,
                            "cache_age_seconds": 0, "cache_outcome": decisions[email],
                        })
                        self._upsert_results(connection, str(job_id), [shared])
                        affected_jobs.add(str(job_id))
                    connection.execute(
                        "DELETE FROM verification_probe_waiters WHERE email=? AND owner_job_id=?",
                        (email, owner_job_id),
                    )
                    connection.execute(
                        "DELETE FROM verification_probe_leases WHERE email=? AND owner_job_id=?",
                        (email, owner_job_id),
                    )
            self._record_cache_metrics(
                connection,
                writes_deliverable=sum(value == "deliverable" for value in decisions.values()),
                writes_permanent_invalid=sum(
                    value == "permanent_invalid" for value in decisions.values()
                ),
                writes_mailbox_full=sum(
                    value == "mailbox_full" for value in decisions.values()
                ),
            )
        return sorted(affected_jobs)

    def register_probe_candidates(
        self, job_id: str, results: list[dict[str, Any]],
    ) -> dict[str, int]:
        pending = [
            (int(result.get("original_index", index)), str(result.get("email") or "").lower())
            for index, result in enumerate(results)
            if result.get("progress_state") == "pending" and result.get("email")
        ]
        if not pending:
            return {"owned": 0, "waiting": 0}
        now = utc_now()
        expires = now + timedelta(seconds=settings.verification_probe_lease_seconds)
        owned: set[str] = set()
        waiting = 0
        with self._lock, closing(self._connect()) as connection:
            begin_immediate(connection)
            connection.execute(
                "DELETE FROM verification_probe_waiters WHERE expires_at<=?",
                (_sql_ts(now),),
            )
            connection.execute(
                "DELETE FROM verification_probe_leases WHERE expires_at<=?",
                (_sql_ts(now),),
            )
            for start in range(0, len(pending), 200):
                batch = pending[start : start + 200]
                values = ", ".join("(?, ?, ?, ?)" for _ in batch)
                parameters: list[Any] = []
                for _index, email in batch:
                    parameters.extend((email, job_id, _sql_ts(now), _sql_ts(expires)))
                rows = connection.execute(f"""
                    INSERT INTO verification_probe_leases(
                        email, owner_job_id, acquired_at, expires_at
                    ) VALUES {values}
                    ON CONFLICT(email) DO UPDATE SET
                        owner_job_id=excluded.owner_job_id,
                        acquired_at=excluded.acquired_at,
                        expires_at=excluded.expires_at
                    WHERE verification_probe_leases.expires_at<=?
                    RETURNING email
                """, (*parameters, _sql_ts(now))).fetchall()
                owned.update(str(row[0]) for row in rows)
            for result_index, email in pending:
                if email in owned:
                    continue
                lease = connection.execute("""
                    SELECT owner_job_id, expires_at FROM verification_probe_leases
                    WHERE email=? AND expires_at>?
                """, (email, _sql_ts(now))).fetchone()
                if lease is None or str(lease[0]) == job_id:
                    continue
                connection.execute("""
                    INSERT INTO verification_probe_waiters(
                        job_id, result_index, email, owner_job_id, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id, result_index) DO UPDATE SET
                        email=excluded.email, owner_job_id=excluded.owner_job_id,
                        created_at=excluded.created_at, expires_at=excluded.expires_at
                """, (
                    job_id, result_index, email, str(lease[0]),
                    _sql_ts(now), lease[1],
                ))
                waiting += 1
            self._record_cache_metrics(connection, coalesced_waiters=waiting)
            connection.commit()
        return {"owned": len(owned), "waiting": waiting}

    @staticmethod
    def _renew_probe_leases_in_connection(
        connection, owner_job_id: str, now: datetime,
    ) -> int:
        expires = _sql_ts(
            now + timedelta(seconds=settings.verification_probe_lease_seconds)
        )
        changed = connection.execute("""
            UPDATE verification_probe_leases SET expires_at=?
            WHERE owner_job_id=? AND expires_at>?
        """, (expires, owner_job_id, _sql_ts(now))).rowcount
        if changed:
            connection.execute("""
                UPDATE verification_probe_waiters SET expires_at=?
                WHERE owner_job_id=? AND expires_at>?
            """, (expires, owner_job_id, _sql_ts(now)))
        return int(changed or 0)

    def renew_probe_leases(self, owner_job_id: str) -> int:
        now = utc_now()
        with closing(self._connect()) as connection:
            return self._renew_probe_leases_in_connection(connection, owner_job_id, now)

    def complete_probe_leases(
        self, owner_job_id: str, results: list[dict[str, Any]],
    ) -> list[str]:
        emails = list(dict.fromkeys(
            str(result.get("email") or "").lower()
            for result in results if result.get("email")
        ))
        if not emails:
            return []
        resumed: set[str] = set()
        with self._lock, closing(self._connect()) as connection:
            begin_immediate(connection)
            for start in range(0, len(emails), 800):
                batch = emails[start : start + 800]
                placeholders = ", ".join("?" for _ in batch)
                rows = connection.execute(f"""
                    SELECT DISTINCT job_id FROM verification_probe_waiters
                    WHERE owner_job_id=? AND email IN ({placeholders})
                """, (owner_job_id, *batch)).fetchall()
                resumed.update(str(row[0]) for row in rows)
                connection.execute(f"""
                    DELETE FROM verification_probe_waiters
                    WHERE owner_job_id=? AND email IN ({placeholders})
                """, (owner_job_id, *batch))
                connection.execute(f"""
                    DELETE FROM verification_probe_leases
                    WHERE owner_job_id=? AND email IN ({placeholders})
                """, (owner_job_id, *batch))
            connection.commit()
        return sorted(resumed)

    def lease_indices(self, job_id: str, worker_id: str, lease_id: str) -> list[int]:
        """Return the immutable result slots covered by one worker lease."""
        with closing(self._connect()) as connection:
            row = connection.execute("""
                SELECT indices_json FROM job_leases
                WHERE id=? AND job_id=? AND worker_id=?
            """, (lease_id, job_id, worker_id)).fetchone()
        if row is None:
            return []
        try:
            return [int(index) for index in (_json_load(row[0]) or [])]
        except (TypeError, ValueError, json.JSONDecodeError):
            return []

    def lease_emails(self, job_id: str, worker_id: str, lease_id: str) -> list[str]:
        """Return the addresses covered by one worker lease without hydrating the job."""
        indices = self.lease_indices(job_id, worker_id, lease_id)
        if not indices:
            return []
        with closing(self._connect()) as connection:
            placeholders = ", ".join("?" for _ in indices)
            rows = connection.execute(f"""
                SELECT email FROM job_results WHERE job_id=?
                    AND original_index IN ({placeholders})
            """, (job_id, *indices)).fetchall()
        return [str(item[0]).lower() for item in rows]

    def release_probe_leases(
        self, owner_job_id: str, emails: list[str] | None = None,
    ) -> list[str]:
        """Release an owner's probes so waiting tasks can be scheduled immediately."""
        keys = list(dict.fromkeys(
            str(email).strip().lower() for email in (emails or []) if str(email).strip()
        ))
        email_clause = ""
        parameters: tuple[Any, ...] = (owner_job_id,)
        if emails is not None:
            if not keys:
                return []
            email_clause = f" AND email IN ({', '.join('?' for _ in keys)})"
            parameters += tuple(keys)
        resumed: set[str] = set()
        with self._lock, closing(self._connect()) as connection:
            begin_immediate(connection)
            rows = connection.execute(f"""
                SELECT DISTINCT job_id FROM verification_probe_waiters
                WHERE owner_job_id=?{email_clause}
            """, parameters).fetchall()
            resumed.update(str(row[0]) for row in rows)
            connection.execute(
                f"DELETE FROM verification_probe_waiters WHERE owner_job_id=?{email_clause}",
                parameters,
            )
            connection.execute(
                f"DELETE FROM verification_probe_leases WHERE owner_job_id=?{email_clause}",
                parameters,
            )
            connection.commit()
        return sorted(resumed)

    def probe_job_ids(self, job_id: str) -> list[str]:
        """Return a visible task and any internal children that can own probes."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT id FROM jobs WHERE id=? OR parent_id=?",
                (job_id, job_id),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def cancel_probe_jobs(self, job_ids: list[str]) -> list[str]:
        """Remove stopped tasks as owners and waiters, returning dependants to wake."""
        keys = list(dict.fromkeys(str(job_id) for job_id in job_ids if job_id))
        if not keys:
            return []
        placeholders = ", ".join("?" for _ in keys)
        resumed: set[str] = set()
        with self._lock, closing(self._connect()) as connection:
            begin_immediate(connection)
            rows = connection.execute(f"""
                SELECT DISTINCT job_id FROM verification_probe_waiters
                WHERE owner_job_id IN ({placeholders})
                    AND job_id NOT IN ({placeholders})
            """, (*keys, *keys)).fetchall()
            resumed.update(str(row[0]) for row in rows)
            connection.execute(f"""
                DELETE FROM verification_probe_waiters
                WHERE owner_job_id IN ({placeholders}) OR job_id IN ({placeholders})
            """, (*keys, *keys))
            connection.execute(f"""
                DELETE FROM verification_probe_leases
                WHERE owner_job_id IN ({placeholders})
            """, tuple(keys))
            connection.commit()
        return sorted(resumed)

    def release_expired_probe_waiters(self) -> list[str]:
        now = _sql_ts(utc_now())
        resumed: set[str] = set()
        with self._lock, closing(self._connect()) as connection:
            begin_immediate(connection)
            rows = connection.execute("""
                SELECT DISTINCT job_id FROM verification_probe_waiters WHERE expires_at<=?
            """, (now,)).fetchall()
            resumed.update(str(row[0]) for row in rows)
            connection.execute(
                "DELETE FROM verification_probe_waiters WHERE expires_at<=?", (now,)
            )
            connection.execute(
                "DELETE FROM verification_probe_leases WHERE expires_at<=?", (now,)
            )
            connection.commit()
        return sorted(resumed)

    def cache_report(self, days: int = 7) -> dict[str, Any]:
        self.flush_cache_metrics()
        days = max(1, min(30, int(days)))
        cutoff_date = utc_now().date() - timedelta(days=days - 1)
        cutoff = cutoff_date if postgres_active() else cutoff_date.isoformat()
        with closing(self._connect()) as connection:
            rows = connection.execute(f"""
                SELECT day, {', '.join(_CACHE_METRIC_NAMES)} FROM verification_cache_days
                WHERE day>=? ORDER BY day
            """, (cutoff,)).fetchall()
            current = connection.execute("""
                SELECT COUNT(*),
                    COALESCE(SUM(CASE WHEN expires_at>? THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN outcome_class='deliverable' THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN outcome_class='permanent_invalid' THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN outcome_class='mailbox_full' THEN 1 ELSE 0 END), 0)
                FROM verification_cache
                WHERE COALESCE(stale_expires_at, expires_at)>?
            """, (_sql_ts(utc_now()), _sql_ts(utc_now()))).fetchone()
        totals = {name: 0 for name in _CACHE_METRIC_NAMES}
        daily = []
        for row in rows:
            item = {"day": str(row[0])}
            for index, name in enumerate(_CACHE_METRIC_NAMES, start=1):
                item[name] = int(row[index] or 0)
                totals[name] += item[name]
            daily.append(item)
        totals["hit_rate"] = round(
            totals["fresh_hits"] / totals["lookups"] * 100, 2
        ) if totals["lookups"] else 0.0
        return {
            "days": days, "daily": daily, "totals": totals,
            "current": {
                "retained": int(current[0] or 0), "fresh": int(current[1] or 0),
                "deliverable": int(current[2] or 0),
                "permanent_invalid": int(current[3] or 0),
                "mailbox_full": int(current[4] or 0),
            },
        }

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
