"""Smoke: rewrite_sql must accept real JobStore claim/lease write-path SQL.

These snippets are lifted from app/db/jobs.py claim_next, claim_remote_lease,
heartbeat_lease, complete_lease_with_results (via _upsert_results), and
abandon_lease. They exercise boolean rewrites, placeholders, and ON CONFLICT.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.pg_compat import rewrite_sql  # noqa: E402


# --- Snippets mirror JobStore write paths (keep in sync with jobs.py) ---

CLAIM_NEXT_SELECT = """
SELECT id, emails_json, worker_count, status, created_at, started_at, finished_at, error,
    results_json, csv_path, owner_id, guest_token_hash, worker_id, heartbeat_at,
    stop_on_deliverable, execution_target, parent_id, retry_parent_id, deferred_retry_at,
    temporary_retry_attempts, list_name
FROM jobs
WHERE status = 'queued' AND execution_target = ?
    AND stop_on_deliverable = 1
    AND (deferred_retry_at IS NULL OR deferred_retry_at <= ?)
ORDER BY created_at LIMIT 1
"""

CLAIM_NEXT_STALE = """
UPDATE jobs SET status = 'queued', worker_id = NULL, heartbeat_at = NULL,
    error = '工作节点已重新领取任务'
WHERE status = 'running' AND heartbeat_at IS NOT NULL AND heartbeat_at < ?
"""

CLAIM_NEXT_MARK_VERIFYING = """
UPDATE job_results SET progress_state='verifying'
WHERE job_id=? AND progress_state='pending'
"""

CLAIM_NEXT_TAKE = """
UPDATE jobs SET status = 'running', worker_id = ?, started_at = ?, heartbeat_at = ?,
    deferred_retry_at = NULL, error = NULL
WHERE id = ?
"""

CLAIM_REMOTE_EXPIRED_LEASES = """
SELECT id, job_id, indices_json FROM job_leases
WHERE completed_at IS NULL AND heartbeat_at < ?
"""

CLAIM_REMOTE_WORKER_NODE = """
INSERT INTO worker_nodes(target, worker_id, capacity, health, last_seen_at)
VALUES (?, ?, ?, 'healthy', ?)
ON CONFLICT(target, worker_id) DO UPDATE SET capacity=excluded.capacity,
    health='healthy', last_seen_at=excluded.last_seen_at
"""

CLAIM_REMOTE_LOAD = """
SELECT COUNT(*) FROM job_leases WHERE worker_id=? AND execution_target=?
    AND completed_at IS NULL AND heartbeat_at >= ?
"""

CLAIM_REMOTE_CANDIDATES = """
SELECT j.id FROM jobs j
LEFT JOIN jobs parent ON parent.id=j.parent_id
LEFT JOIN scheduler_owner_turns turn ON turn.target=?
    AND turn.owner_key=COALESCE(parent.owner_id, j.owner_id, j.id)
WHERE j.status IN ('queued', 'running') AND j.execution_target IN (?, ?)
    AND j.stop_on_deliverable = 0
    AND (j.deferred_retry_at IS NULL OR j.deferred_retry_at <= ?)
    AND EXISTS (SELECT 1 FROM job_results r WHERE r.job_id=j.id
        AND r.progress_state='pending')
ORDER BY CASE WHEN j.execution_target=? THEN 0 ELSE 1 END,
    COALESCE(turn.last_claimed_at, '1970-01-01T00:00:00+00:00'), j.created_at
LIMIT ?
"""

CLAIM_REMOTE_OWNER_TURN = """
INSERT INTO scheduler_owner_turns(target, owner_key, last_claimed_at) VALUES (?, ?, ?)
ON CONFLICT(target, owner_key) DO UPDATE SET last_claimed_at=excluded.last_claimed_at
"""

CLAIM_REMOTE_INSERT_LEASE = """
INSERT INTO job_leases(id, job_id, worker_id, execution_target, indices_json, claimed_at, heartbeat_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
"""

CLAIM_REMOTE_MARK_INDICES = """
UPDATE job_results SET progress_state='verifying' WHERE job_id=?
AND original_index IN (?, ?, ?) AND progress_state='pending'
"""

CLAIM_REMOTE_JOB_RUNNING = """
UPDATE jobs SET status='running', worker_id=?, started_at=COALESCE(started_at, ?),
    heartbeat_at=?, deferred_retry_at=NULL, error=NULL WHERE id=?
"""

HEARTBEAT_LEASE = """
UPDATE job_leases SET heartbeat_at=? WHERE id=? AND job_id=? AND worker_id=?
    AND completed_at IS NULL AND heartbeat_at >= ?
"""

HEARTBEAT_MX = """
UPDATE mx_scheduler_leases SET expires_at=? WHERE lease_id=?
"""

HEARTBEAT_JOB = """
UPDATE jobs SET heartbeat_at=? WHERE id=?
"""

UPSERT_RESULTS = """
INSERT INTO job_results(job_id, original_index, email, progress_state, result_json, updated_at,
    deliverability, is_valid, is_skipped, is_catch_all, retry_at, retry_updated, query_fields_ready)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(job_id, original_index) DO UPDATE SET
    email=excluded.email, progress_state=excluded.progress_state,
    result_json=excluded.result_json, updated_at=excluded.updated_at,
    deliverability=excluded.deliverability, is_valid=excluded.is_valid,
    is_skipped=excluded.is_skipped, is_catch_all=excluded.is_catch_all,
    retry_at=excluded.retry_at, retry_updated=excluded.retry_updated,
    query_fields_ready=excluded.query_fields_ready
WHERE job_results.progress_state IN ('pending', 'verifying')
    OR job_results.progress_state = excluded.progress_state
"""

COMPLETE_LEASE = """
UPDATE job_leases SET completed_at=? WHERE id=? AND job_id=? AND worker_id=?
    AND completed_at IS NULL
"""

COMPLETE_LEASE_SELECT = """
SELECT indices_json FROM job_leases WHERE id=? AND job_id=? AND worker_id=?
    AND completed_at IS NULL AND heartbeat_at >= ?
"""

ABANDON_SELECT = """
SELECT indices_json FROM job_leases
WHERE id=? AND job_id=? AND worker_id=? AND completed_at IS NULL AND heartbeat_at >= ?
"""

ABANDON_RELEASE_INDICES = """
UPDATE job_results SET progress_state='pending' WHERE job_id=?
AND original_index IN (?, ?) AND progress_state='verifying'
"""

ABANDON_REQUEUE_JOB = """
UPDATE jobs SET status='queued', worker_id=NULL, heartbeat_at=NULL WHERE id=? AND status='running'
"""

RELEASE_ORPHANED = """
UPDATE job_results SET progress_state='pending'
WHERE progress_state='verifying'
  AND NOT EXISTS (
      SELECT 1 FROM job_leases lease
      WHERE lease.job_id=job_results.job_id
        AND lease.completed_at IS NULL AND lease.heartbeat_at >= ?
  )
"""


def _assert_pg_shape(sql: str, *, expect_true: bool = False, expect_false: bool = False) -> str:
    out = rewrite_sql(sql)
    assert "?" not in out, f"untranslated placeholder remains:\n{out}"
    assert "%s" in out or "TRUE" in out.upper() or "FALSE" in out.upper() or "SELECT" in out.upper()
    if expect_true:
        assert "stop_on_deliverable = TRUE" in out or "stop_on_deliverable=TRUE" in out.replace(" ", "")
        assert "stop_on_deliverable = 1" not in out
    if expect_false:
        assert "stop_on_deliverable = FALSE" in out
        assert "stop_on_deliverable = 0" not in out
    return out


def test_claim_next_stop_on_deliverable_true() -> None:
    out = _assert_pg_shape(CLAIM_NEXT_SELECT, expect_true=True)
    assert "deferred_retry_at <= %s" in out
    assert "execution_target = %s" in out


def test_claim_next_stale_and_take() -> None:
    stale = _assert_pg_shape(CLAIM_NEXT_STALE)
    assert "heartbeat_at < %s" in stale
    take = _assert_pg_shape(CLAIM_NEXT_TAKE)
    assert take.count("%s") == 4
    mark = _assert_pg_shape(CLAIM_NEXT_MARK_VERIFYING)
    assert "progress_state" in mark


def test_claim_remote_stop_on_deliverable_false() -> None:
    out = _assert_pg_shape(CLAIM_REMOTE_CANDIDATES, expect_false=True)
    assert "ORDER BY" in out
    assert "COALESCE(turn.last_claimed_at" in out
    assert out.count("%s") >= 5


def test_claim_remote_on_conflict_upserts() -> None:
    worker = rewrite_sql(CLAIM_REMOTE_WORKER_NODE)
    assert "?" not in worker
    assert "ON CONFLICT (" in worker
    assert "excluded.capacity" in worker
    assert "%s" in worker

    turns = rewrite_sql(CLAIM_REMOTE_OWNER_TURN)
    assert "ON CONFLICT (" in turns
    assert "excluded.last_claimed_at" in turns
    assert "?" not in turns


def test_claim_remote_lease_and_indices() -> None:
    expired = _assert_pg_shape(CLAIM_REMOTE_EXPIRED_LEASES)
    assert "heartbeat_at < %s" in expired
    load = _assert_pg_shape(CLAIM_REMOTE_LOAD)
    assert load.count("%s") == 3
    insert = _assert_pg_shape(CLAIM_REMOTE_INSERT_LEASE)
    assert insert.count("%s") == 7
    mark = _assert_pg_shape(CLAIM_REMOTE_MARK_INDICES)
    assert mark.count("%s") == 4
    running = _assert_pg_shape(CLAIM_REMOTE_JOB_RUNNING)
    assert "COALESCE(started_at, %s)" in running


def test_heartbeat_lease() -> None:
    hb = _assert_pg_shape(HEARTBEAT_LEASE)
    assert "heartbeat_at >= %s" in hb
    assert hb.count("%s") == 5
    _assert_pg_shape(HEARTBEAT_MX)
    _assert_pg_shape(HEARTBEAT_JOB)


def test_complete_lease_upsert_on_conflict() -> None:
    upsert = rewrite_sql(UPSERT_RESULTS)
    assert "?" not in upsert
    assert "ON CONFLICT (" in upsert
    assert "excluded.result_json" in upsert
    assert "job_results.progress_state" in upsert
    # 13 bind params for VALUES
    assert upsert.count("%s") == 13

    complete = _assert_pg_shape(COMPLETE_LEASE)
    assert complete.count("%s") == 4
    select = _assert_pg_shape(COMPLETE_LEASE_SELECT)
    assert "heartbeat_at >= %s" in select


def test_abandon_lease() -> None:
    sel = _assert_pg_shape(ABANDON_SELECT)
    assert sel.count("%s") == 4
    rel = _assert_pg_shape(ABANDON_RELEASE_INDICES)
    assert rel.count("%s") == 3
    requeue = _assert_pg_shape(ABANDON_REQUEUE_JOB)
    assert "status='running'" in requeue or "status = 'running'" in requeue
    orphan = _assert_pg_shape(RELEASE_ORPHANED)
    assert "heartbeat_at >= %s" in orphan


def test_worker_node_timestamps_bind_via_sql_ts() -> None:
    """PG timestamptz compared to isoformat() text marks every node offline.

    str(datetime) is 'YYYY-MM-DD HH:MM...'; isoformat() is 'YYYY-MM-DDTHH:MM...'.
    Space < 'T', so a fresh last_seen_at looks older than any same-day cutoff.
    """
    import inspect
    from datetime import datetime, timezone

    from app.db.jobs import JobStore

    now = datetime(2026, 8, 13, 12, 34, 56, tzinfo=timezone.utc)
    assert str(now)[10] == " "
    assert now.isoformat()[10] == "T"
    assert str(now) < now.isoformat()

    seen = inspect.getsource(JobStore.record_worker_seen)
    recon = inspect.getsource(JobStore.reconcile_worker_nodes)
    valid = inspect.getsource(JobStore.lease_valid)
    requeue = inspect.getsource(JobStore.requeue_stale_jobs)
    summary = inspect.getsource(JobStore.health_summary)
    assert "_sql_ts" in seen
    assert "utc_now().isoformat()" not in seen
    assert "_sql_ts" in recon
    assert ".isoformat()" not in recon
    assert "_sql_ts" in valid
    assert ".isoformat()" not in valid
    assert "_sql_ts" in requeue
    assert "str(cooldown) > utc_now().isoformat()" not in summary


def test_no_sqlite_only_control_left() -> None:
    """Claim paths must not rely on PRAGMA / INSERT OR * after rewrite."""
    for sql in (
        CLAIM_REMOTE_WORKER_NODE,
        CLAIM_REMOTE_OWNER_TURN,
        UPSERT_RESULTS,
    ):
        out = rewrite_sql(sql).upper()
        assert "INSERT OR " not in out
        assert "PRAGMA" not in out
        assert "COLLATE" not in out


def test_remote_claim_batches_scheduler_lookups() -> None:
    """The long-poll write transaction must not query once per email."""
    import inspect

    from app.db.jobs import JobStore

    source = inspect.getsource(JobStore.claim_remote_lease)
    assert "_release_orphaned_results" not in source
    assert "_scheduler_key_for_email(connection" not in source
    assert "_scheduler_profile_is_cooling_down(connection" not in source
    assert "_scheduler_profile_limit(connection" not in source
    assert "FROM scheduler_domain_routes" in source
    assert "FROM scheduler_domain_profiles" in source
    assert "candidate_scan_limit" in source
    assert "_release_orphaned_results" not in inspect.getsource(JobStore.claim_next)


def test_public_mailbox_scheduler_recovers_on_its_own_threshold() -> None:
    import inspect

    from app.config import settings
    from app.db.jobs import JobStore

    assert JobStore._scheduler_successes_per_step("gmail", prospecting=False) == (
        settings.scheduler_gmail_successes_per_step
    )
    assert JobStore._scheduler_successes_per_step("microsoft", prospecting=False) == (
        settings.scheduler_microsoft_successes_per_step
    )
    assert JobStore._scheduler_successes_per_step("domain:example.com", prospecting=False) == (
        settings.scheduler_successes_per_step
    )
    summary = inspect.getsource(JobStore.health_summary)
    assert "scheduler_runtime" in summary
    assert "completed_last_60_seconds" in summary
    assert "timings_ms_last_60_seconds" in summary
    assert "active_lease_age_p95_seconds" in summary


def test_postgres_session_timeout_is_set_at_connect() -> None:
    """A pooled checkout must not send a session SET round trip."""
    import inspect

    from app.db.pg_compat import PgConnection
    from app.db.postgresql import connect

    assert "statement_timeout=15000" in inspect.getsource(connect)
    assert "SET statement_timeout" not in inspect.getsource(PgConnection)


def test_remote_claim_long_poll_is_database_bounded() -> None:
    source = (ROOT / "app" / "api" / "routes.py").read_text(encoding="utf-8")
    assert "await asyncio.sleep(wait_seconds)" in source
    assert "await asyncio.sleep(min(2.0, remaining))" in source
    assert "min(0.25" not in source


def test_background_workers_use_dedicated_postgres_tunnel() -> None:
    worker_unit = (ROOT / "deploy" / "verigo-worker@.service").read_text(encoding="utf-8")
    supervisor_unit = (ROOT / "deploy" / "verigo-supervisor.service").read_text(encoding="utf-8")
    tunnel_unit = (
        ROOT / "deploy" / "verigo-postgres-worker-tunnel.service"
    ).read_text(encoding="utf-8")
    release = (ROOT / "deploy" / "release.sh").read_text(encoding="utf-8")
    assert "verigo-postgres-worker-tunnel.service" in worker_unit
    assert "EnvironmentFile=/etc/verigo/verigo-worker.env" in worker_unit
    assert "verigo-postgres-worker-tunnel.service" in supervisor_unit
    assert "127.0.0.1:15433:127.0.0.1:5432" in tunnel_unit
    assert "write_worker_env" in release
    assert "127.0.0.1:15433" in release


def test_cloudshell_manifest_sync_is_batched_and_throttled() -> None:
    import inspect

    from app.core.cloudshell_coordinator import CloudShellCoordinator

    sync_source = inspect.getsource(CloudShellCoordinator.sync_accounts)
    refresh_source = inspect.getsource(CloudShellCoordinator.refresh_pool)
    assert "account_placeholders" in sync_source
    assert "db.executemany" not in sync_source
    assert "_accounts_synced_at >= 60" in refresh_source
    assert "_pool_refreshed_at < 60" in refresh_source
    assert "self._pool_refreshed_at = now_monotonic" in refresh_source


def test_cloudshell_reservation_commit_does_not_try_global_lock() -> None:
    import inspect

    from app.core.cloudshell_coordinator import CloudShellCoordinator

    source = inspect.getsource(CloudShellCoordinator.commit)
    assert "begin_immediate(db)" in source
    assert "_begin_write" not in source


def main() -> int:
    tests = [
        test_claim_next_stop_on_deliverable_true,
        test_claim_next_stale_and_take,
        test_claim_remote_stop_on_deliverable_false,
        test_claim_remote_on_conflict_upserts,
        test_claim_remote_lease_and_indices,
        test_heartbeat_lease,
        test_complete_lease_upsert_on_conflict,
        test_abandon_lease,
        test_worker_node_timestamps_bind_via_sql_ts,
        test_no_sqlite_only_control_left,
        test_remote_claim_batches_scheduler_lookups,
        test_postgres_session_timeout_is_set_at_connect,
        test_remote_claim_long_poll_is_database_bounded,
        test_background_workers_use_dedicated_postgres_tunnel,
        test_cloudshell_manifest_sync_is_batched_and_throttled,
        test_cloudshell_reservation_commit_does_not_try_global_lock,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"OK  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    if failed:
        print(f"{failed} failed")
        return 1
    print("all postgres_claim_rewrite smoke tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
