"""Run a bounded, internal verification load test with synthetic `.invalid` data.

The test creates exactly 1,000 addresses by default: 250 cache hits and 250
live misses for each of the Gmail, QQ, and local execution targets.  It creates
no user-visible task, CSV, notification, credit entry, or result object.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings
from app.db.jobs import job_store
from app.db.postgresql import connection, resolve_database_url
from app.tasks.verification import verification_tasks


LOAD_LIST_NAME = "__rollout_load_test__"


@dataclass(frozen=True)
class TaskReport:
    target: str
    job_id: str
    status: str
    total: int
    completed: int
    pending: int
    cache_hits: int
    p95_seconds: float | None


def _email(prefix: str, group: str, index: int) -> str:
    return f"{prefix}-{group}-{index:04d}@example.invalid"


def _cache_result(email: str) -> dict[str, object]:
    return {
        "email": email,
        "valid": True,
        "deliverable": True,
        "smtp_code": "250",
        "verification_method": "internal load-test cache",
        "smtp_result": "250 synthetic cache result",
        "message": "internal synthetic cache result",
        "progress_state": "completed",
    }


def _reports(job_ids: Iterable[tuple[str, str]]) -> tuple[list[TaskReport], int, int, int]:
    ids = [job_id for _target, job_id in job_ids]
    if not ids:
        return [], 0, 0, 0
    with connection(resolve_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT job.id, job.execution_target, job.status,
                       COUNT(result.original_index) AS total,
                       COUNT(*) FILTER (
                           WHERE result.progress_state IN ('completed', 'failed', 'stopped')
                       ) AS completed,
                       COUNT(*) FILTER (
                           WHERE result.progress_state IN ('pending', 'verifying')
                       ) AS pending,
                       COUNT(*) FILTER (
                           WHERE result.result_json->>'cache_hit' = 'true'
                       ) AS cache_hits,
                       percentile_cont(0.95) WITHIN GROUP (
                           ORDER BY EXTRACT(EPOCH FROM (result.updated_at - job.created_at))
                       ) FILTER (
                           WHERE result.progress_state IN ('completed', 'failed', 'stopped')
                       ) AS p95_seconds
                FROM jobs AS job
                LEFT JOIN job_results AS result ON result.job_id = job.id
                WHERE job.id = ANY(%s)
                GROUP BY job.id, job.execution_target, job.status
                ORDER BY job.execution_target, job.id
                """,
                (ids,),
            )
            rows = {str(row["id"]): row for row in cur.fetchall()}
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE completed_at IS NULL) AS active_leases,
                    (SELECT COUNT(*) FROM verification_probe_leases) AS probe_leases,
                    (SELECT COUNT(*) FROM verification_probe_waiters) AS probe_waiters
                FROM job_leases
                """
            )
            active = cur.fetchone()
    reports = []
    for target, job_id in job_ids:
        row = rows[job_id]
        p95 = row["p95_seconds"]
        reports.append(
            TaskReport(
                target=target,
                job_id=job_id,
                status=str(row["status"]),
                total=int(row["total"] or 0),
                completed=int(row["completed"] or 0),
                pending=int(row["pending"] or 0),
                cache_hits=int(row["cache_hits"] or 0),
                p95_seconds=round(float(p95), 2) if p95 is not None else None,
            )
        )
    return (
        reports,
        int(active["active_leases"] or 0),
        int(active["probe_leases"] or 0),
        int(active["probe_waiters"] or 0),
    )


def _cleanup_cache(prefix: str) -> None:
    pattern = f"{prefix}-%@example.invalid"
    with connection(resolve_database_url()) as conn:
        with conn.cursor() as cur:
            # This prefix is generated per test invocation and cannot match a
            # customer address. Remove cache rows so test data is never reused.
            cur.execute("DELETE FROM verification_cache WHERE email LIKE %s", (pattern,))
            cur.execute("DELETE FROM verified_emails WHERE email LIKE %s", (pattern,))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--per-group", type=int, default=250)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--keep-cache", action="store_true")
    args = parser.parse_args()
    per_group = max(1, args.per_group)
    timeout_seconds = max(30, args.timeout_seconds)
    prefix = f"verigo-load-{int(time.time())}-{uuid.uuid4().hex[:6]}"

    cached_emails = [_email(prefix, "cache", index) for index in range(per_group)]
    gmail_misses = [_email(prefix, "gmail", index) for index in range(per_group)]
    qq_misses = [_email(prefix, "qq", index) for index in range(per_group)]
    local_misses = [_email(prefix, "local", index) for index in range(per_group)]
    total = per_group * 4
    started = time.monotonic()
    task_ids: list[tuple[str, str]] = []
    max_active_leases = 0

    try:
        job_store.cache_results([_cache_result(email) for email in cached_emails])
        cached = job_store.cached_results(cached_emails)
        if len(cached) != per_group:
            raise RuntimeError("synthetic cache setup did not return every expected hit")

        gmail = verification_tasks.submit(
            cached_emails + gmail_misses,
            settings.cloudshell_worker_max_workers,
            owner_id=None,
            execution_target="gmail",
            immediate_results=list(cached.values()),
            list_name=LOAD_LIST_NAME,
            is_cache_refresh=True,
        )
        qq = verification_tasks.submit(
            qq_misses,
            settings.qq_worker_max_workers,
            owner_id=None,
            execution_target="tencent_qq",
            list_name=LOAD_LIST_NAME,
            is_cache_refresh=True,
        )
        local = verification_tasks.submit(
            local_misses,
            settings.max_workers_per_job,
            owner_id=None,
            execution_target="local",
            list_name=LOAD_LIST_NAME,
            is_cache_refresh=True,
        )
        task_ids = [("gmail", gmail.id), ("tencent_qq", qq.id), ("local", local.id)]

        # The cache half of the Gmail job is materialized synchronously. This
        # catches a regression before workers are asked to process live misses.
        initial, active_leases, _probe_leases, _probe_waiters = _reports(task_ids)
        gmail_initial = next(report for report in initial if report.job_id == gmail.id)
        if gmail_initial.cache_hits != per_group or gmail_initial.completed < per_group:
            raise RuntimeError("cache partition was not completed synchronously")
        max_active_leases = max(max_active_leases, active_leases)

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            reports, active_leases, probe_leases, probe_waiters = _reports(task_ids)
            max_active_leases = max(max_active_leases, active_leases)
            if all(report.status == "completed" and report.pending == 0 for report in reports):
                break
            if any(report.status in {"failed", "stopped"} for report in reports):
                break
            time.sleep(2)
        reports, active_leases, probe_leases, probe_waiters = _reports(task_ids)
        elapsed_seconds = round(time.monotonic() - started, 2)
        success = (
            sum(report.total for report in reports) == total
            and all(report.status == "completed" and report.pending == 0 for report in reports)
            and sum(report.cache_hits for report in reports) == per_group
            and probe_leases == 0
            and probe_waiters == 0
        )
        print(json.dumps({
            "kind": "internal_synthetic_mixed_load",
            "addresses": total,
            "cache_hits_expected": per_group,
            "elapsed_seconds": elapsed_seconds,
            "max_active_worker_leases": max_active_leases,
            "active_worker_leases_at_finish": active_leases,
            "probe_leases_at_finish": probe_leases,
            "probe_waiters_at_finish": probe_waiters,
            "tasks": [
                {
                    "target": report.target,
                    "status": report.status,
                    "total": report.total,
                    "completed": report.completed,
                    "pending": report.pending,
                    "cache_hits": report.cache_hits,
                    "p95_seconds": report.p95_seconds,
                }
                for report in reports
            ],
            "success": success,
        }, sort_keys=True))
        return 0 if success else 1
    finally:
        if not args.keep_cache:
            _cleanup_cache(prefix)


if __name__ == "__main__":
    raise SystemExit(main())
