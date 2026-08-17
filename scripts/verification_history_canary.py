"""Run a bounded SMTP review canary using previously verified addresses.

The canary is intentionally internal: it creates cache-refresh jobs, never
creates an account task, consumes credits, emits notifications, or writes a
CSV. Raw addresses are used only in memory and are never printed in reports.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.routes import email_execution_target, submit_routed_job
from app.config import settings
from app.core.smtp_cross_route import decision_for as cross_route_decision
from app.db.postgresql import connection, resolve_database_url
from app.tasks.verification import SMTP_REVIEW_CANARY_LIST_NAME


EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+$")
REMOTE_REVIEW_TARGETS = frozenset({"gmail", "codearts", "cloudstudio_domestic"})
EXCLUDED_DOMAINS = frozenset({
    "qq.com", "vip.qq.com", "foxmail.com",
    "outlook.com", "hotmail.com", "live.com", "msn.com",
})


@dataclass(frozen=True)
class Candidate:
    email: str
    cohort: str
    baseline: str
    source_target: str


@dataclass(frozen=True)
class StageReport:
    addresses: int
    elapsed_seconds: float
    completed_in_60_seconds: int
    completed_total: int
    p50_seconds: float
    p95_seconds: float


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].strip().lower().rstrip(".") if "@" in email else ""


def _excluded(email: str) -> bool:
    domain = _domain(email)
    return bool(
        not EMAIL_PATTERN.fullmatch(email)
        or domain in EXCLUDED_DOMAINS
        or domain.startswith(("outlook.", "hotmail.", "live.", "msn.", "yahoo."))
        or domain.endswith((".outlook.com", ".hotmail.com", ".live.com", ".msn.com"))
        or domain in {"ymail.com", "rocketmail.com"}
    )


def _payload(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _baseline(payload: dict[str, Any], fallback: str = "unknown") -> str:
    if payload.get("deliverable") is True:
        return "deliverable"
    if payload.get("deliverable") is False:
        return "undeliverable"
    return fallback


def _active_user_jobs() -> int:
    with connection(resolve_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) AS jobs FROM jobs
                WHERE (status='running' OR (
                    status='queued'
                    AND (deferred_retry_at IS NULL OR deferred_retry_at <= CURRENT_TIMESTAMP)
                ))
                  AND is_cache_refresh IS NOT TRUE"""
            )
            return int(cur.fetchone()["jobs"] or 0)


def wait_for_quiet(*, quiet_seconds: int, timeout_seconds: int) -> None:
    deadline = time.monotonic() + max(quiet_seconds, timeout_seconds)
    quiet_since: float | None = None
    while time.monotonic() < deadline:
        if _active_user_jobs() == 0:
            quiet_since = quiet_since or time.monotonic()
            if time.monotonic() - quiet_since >= quiet_seconds:
                return
        else:
            quiet_since = None
        time.sleep(2)
    raise TimeoutError("production queue did not become quiet before the canary deadline")


def _active_emails() -> set[str]:
    with connection(resolve_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT LOWER(result.email) AS email
                FROM job_results AS result
                JOIN jobs AS job ON job.id=result.job_id
                WHERE job.status IN ('queued', 'running')"""
            )
            return {str(row["email"]) for row in cur.fetchall()}


def _historical_4xx_rows(*, lookback_days: int, limit: int, seed: str) -> list[dict[str, Any]]:
    with connection(resolve_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """WITH latest AS (
                    SELECT DISTINCT ON (LOWER(result.email))
                        LOWER(result.email) AS email,
                        result.result_json
                    FROM job_results AS result
                    JOIN jobs AS job ON job.id=result.job_id
                    WHERE result.updated_at >= CURRENT_TIMESTAMP - (%s * INTERVAL '1 day')
                      AND job.is_cache_refresh IS NOT TRUE
                      AND COALESCE(result.result_json->>'smtp_code', '') LIKE '4%%'
                    ORDER BY LOWER(result.email), result.updated_at DESC
                )
                SELECT email, result_json FROM latest
                ORDER BY MD5(email || %s) LIMIT %s""",
                (lookback_days, seed, limit),
            )
            return list(cur.fetchall())


def _stable_rows(*, lookback_days: int, limit: int, seed: str) -> list[dict[str, Any]]:
    with connection(resolve_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT LOWER(email) AS email, result_json, outcome_class
                FROM verification_cache
                WHERE COALESCE(verified_at, updated_at)
                        >= CURRENT_TIMESTAMP - (%s * INTERVAL '1 day')
                  AND outcome_class IN ('deliverable', 'permanent_invalid', 'mailbox_full')
                ORDER BY MD5(LOWER(email) || %s) LIMIT %s""",
                (lookback_days, seed, limit),
            )
            return list(cur.fetchall())


def sample_candidates(
    *, total: int, fourxx: int, per_domain: int, lookback_days: int, seed: str,
) -> list[Candidate]:
    active = _active_emails()
    selected: list[Candidate] = []
    selected_emails: set[str] = set()
    domain_counts: Counter[str] = Counter()

    def add(candidate: Candidate) -> bool:
        email = candidate.email.lower()
        domain = _domain(email)
        if (
            email in active
            or email in selected_emails
            or _excluded(email)
            or domain_counts[domain] >= per_domain
        ):
            return False
        selected.append(candidate)
        selected_emails.add(email)
        domain_counts[domain] += 1
        return True

    rows = _historical_4xx_rows(
        lookback_days=lookback_days,
        limit=max(2000, fourxx * 30),
        seed=f"{seed}:4xx",
    )
    for row in rows:
        email = str(row["email"] or "").lower()
        if _excluded(email):
            continue
        target = email_execution_target(email, None)
        if target not in REMOTE_REVIEW_TARGETS:
            continue
        payload = _payload(row["result_json"])
        payload.pop("cross_route_attempts", None)
        decision = cross_route_decision(email, payload, source_target=target)
        if not decision.eligible:
            continue
        add(Candidate(email, "historical_4xx", "temporary_4xx", target))
        if sum(candidate.cohort == "historical_4xx" for candidate in selected) >= fourxx:
            break

    selected_fourxx = sum(
        candidate.cohort == "historical_4xx" for candidate in selected
    )
    if selected_fourxx != fourxx:
        raise RuntimeError(
            f"only {selected_fourxx} eligible historical 4xx candidates were available "
            f"for {fourxx} requested"
        )

    stable_needed = total - len(selected)
    rows = _stable_rows(
        lookback_days=lookback_days,
        limit=max(5000, stable_needed * 40),
        seed=f"{seed}:stable",
    )
    for row in rows:
        email = str(row["email"] or "").lower()
        target = email_execution_target(email, None)
        if target not in REMOTE_REVIEW_TARGETS:
            continue
        payload = _payload(row["result_json"])
        if add(Candidate(email, "stable", _baseline(payload), target)) and len(selected) >= total:
            break

    if len(selected) != total:
        raise RuntimeError(
            f"only {len(selected)} privacy-bounded candidates were available for {total} requested"
        )
    return selected


def _stage_stats(job_id: str) -> dict[str, Any]:
    with connection(resolve_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT job.status,
                    COUNT(result.original_index) AS total,
                    COUNT(*) FILTER (
                        WHERE result.progress_state IN ('completed', 'failed', 'stopped')
                    ) AS completed,
                    COUNT(*) FILTER (
                        WHERE result.progress_state IN ('pending', 'verifying')
                    ) AS pending,
                    COUNT(*) FILTER (
                        WHERE result.initial_completed_at
                            <= job.created_at + INTERVAL '60 seconds'
                    ) AS completed_60,
                    COALESCE(PERCENTILE_CONT(0.5) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM (result.initial_completed_at-job.created_at))
                    ) FILTER (WHERE result.initial_completed_at IS NOT NULL), 0) AS p50,
                    COALESCE(PERCENTILE_CONT(0.95) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM (result.initial_completed_at-job.created_at))
                    ) FILTER (WHERE result.initial_completed_at IS NOT NULL), 0) AS p95
                FROM jobs AS job
                LEFT JOIN job_results AS result ON result.job_id=job.id
                WHERE job.id=%s GROUP BY job.id, job.status""",
                (job_id,),
            )
            row = cur.fetchone()
    if row is None:
        raise RuntimeError("canary root job disappeared")
    return dict(row)


def wait_for_stage(job_id: str, *, timeout_seconds: int) -> StageReport:
    started = time.monotonic()
    deadline = started + timeout_seconds
    while time.monotonic() < deadline:
        row = _stage_stats(job_id)
        if str(row["status"]) in {"failed", "stopped"}:
            raise RuntimeError(f"canary stage ended as {row['status']}")
        if str(row["status"]) == "completed" and int(row["pending"] or 0) == 0:
            return StageReport(
                addresses=int(row["total"] or 0),
                elapsed_seconds=round(time.monotonic() - started, 2),
                completed_in_60_seconds=int(row["completed_60"] or 0),
                completed_total=int(row["completed"] or 0),
                p50_seconds=round(float(row["p50"] or 0), 2),
                p95_seconds=round(float(row["p95"] or 0), 2),
            )
        time.sleep(2)
    raise TimeoutError("canary stage did not complete before its deadline")


def _review_counts(job_ids: list[str]) -> dict[str, int]:
    if not job_ids:
        return {}
    with connection(resolve_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT event_type, COUNT(*) AS events
                FROM smtp_review_events WHERE parent_job_id=ANY(%s)
                GROUP BY event_type""",
                (job_ids,),
            )
            return {str(row["event_type"]): int(row["events"] or 0) for row in cur.fetchall()}


def wait_for_reviews(job_ids: list[str], *, timeout_seconds: int) -> bool:
    deadline = time.monotonic() + max(10, timeout_seconds)
    stable_since: float | None = None
    while time.monotonic() < deadline:
        counts = _review_counts(job_ids)
        scheduled = counts.get("scheduled", 0)
        settled = counts.get("completed", 0) + counts.get("worker_failed", 0)
        if scheduled == 0 or settled >= scheduled:
            stable_since = stable_since or time.monotonic()
            if time.monotonic() - stable_since >= 10:
                return True
        else:
            stable_since = None
        time.sleep(2)
    return False


def _result_matrix(candidates: Iterable[Candidate], job_ids: list[str]) -> dict[str, int]:
    baseline = {candidate.email: candidate.baseline for candidate in candidates}
    with connection(resolve_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT LOWER(email) AS email, deliverability
                FROM job_results WHERE job_id=ANY(%s)""",
                (job_ids,),
            )
            rows = cur.fetchall()
    matrix: Counter[str] = Counter()
    for row in rows:
        value = row["deliverability"]
        current = "deliverable" if value == 1 else "undeliverable" if value == 0 else "unknown"
        matrix[f"{baseline.get(str(row['email']), 'unknown')}->{current}"] += 1
    return dict(sorted(matrix.items()))


def _review_report(job_ids: list[str]) -> dict[str, Any]:
    with connection(resolve_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT event_type, COALESCE(outcome, '') AS outcome,
                    COALESCE(decision_reason, '') AS reason, COUNT(*) AS events
                FROM smtp_review_events WHERE parent_job_id=ANY(%s)
                GROUP BY event_type, COALESCE(outcome, ''), COALESCE(decision_reason, '')
                ORDER BY event_type, outcome, reason""",
                (job_ids,),
            )
            rows = cur.fetchall()
            cur.execute(
                """SELECT COUNT(*) AS samples,
                    COALESCE(AVG(latency_ms), 0) AS average,
                    COALESCE(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY latency_ms), 0) AS p50,
                    COALESCE(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms), 0) AS p95
                FROM smtp_review_events
                WHERE parent_job_id=ANY(%s)
                  AND event_type='completed' AND latency_ms IS NOT NULL""",
                (job_ids,),
            )
            latency = cur.fetchone()
    return {
        "events": [
            {
                "event_type": str(row["event_type"]),
                "outcome": str(row["outcome"]),
                "reason": str(row["reason"]),
                "count": int(row["events"] or 0),
            }
            for row in rows
        ],
        "latency_ms": {
            "samples": int(latency["samples"] or 0),
            "average": round(float(latency["average"] or 0), 1),
            "p50": round(float(latency["p50"] or 0), 1),
            "p95": round(float(latency["p95"] or 0), 1),
        },
    }


def stop_unfinished_canary(started_at: datetime) -> int:
    with connection(resolve_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id FROM jobs
                WHERE list_name=%s AND created_at>=%s
                  AND status IN ('queued', 'running')""",
                (SMTP_REVIEW_CANARY_LIST_NAME, started_at - timedelta(seconds=1)),
            )
            job_ids = [str(row["id"]) for row in cur.fetchall()]
            if not job_ids:
                return 0
            cur.execute(
                """DELETE FROM mx_scheduler_leases WHERE lease_id IN (
                    SELECT id FROM job_leases WHERE job_id=ANY(%s) AND completed_at IS NULL
                )""",
                (job_ids,),
            )
            cur.execute(
                """UPDATE job_leases SET completed_at=CURRENT_TIMESTAMP
                WHERE job_id=ANY(%s) AND completed_at IS NULL""",
                (job_ids,),
            )
            cur.execute(
                """UPDATE jobs SET status='stopped', finished_at=CURRENT_TIMESTAMP,
                    worker_id=NULL, heartbeat_at=NULL,
                    error='Internal SMTP review canary window ended'
                WHERE id=ANY(%s) AND status IN ('queued', 'running')""",
                (job_ids,),
            )
            return int(cur.rowcount or 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--total", type=int, default=2000)
    parser.add_argument("--fourxx", type=int, default=500)
    parser.add_argument("--stage-size", type=int, default=500)
    parser.add_argument("--per-domain", type=int, default=8)
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--quiet-seconds", type=int, default=15)
    parser.add_argument("--quiet-timeout-seconds", type=int, default=900)
    parser.add_argument("--stage-timeout-seconds", type=int, default=1800)
    parser.add_argument("--review-timeout-seconds", type=int, default=900)
    parser.add_argument("--seed", default="")
    parser.add_argument("--confirm-production", action="store_true")
    args = parser.parse_args()
    if not args.confirm_production:
        parser.error("--confirm-production is required")
    total = max(1, min(5000, args.total))
    fourxx = max(0, min(total, args.fourxx))
    stage_size = max(1, min(1000, args.stage_size))
    per_domain = max(1, min(50, args.per_domain))
    seed = args.seed.strip() or uuid.uuid4().hex
    started_at = utc_now()
    candidates: list[Candidate] = []
    root_ids: list[str] = []
    stage_reports: list[StageReport] = []
    reviews_settled = False

    try:
        wait_for_quiet(
            quiet_seconds=max(0, args.quiet_seconds),
            timeout_seconds=max(30, args.quiet_timeout_seconds),
        )
        candidates = sample_candidates(
            total=total,
            fourxx=fourxx,
            per_domain=per_domain,
            lookback_days=max(1, min(365, args.lookback_days)),
            seed=seed,
        )
        for offset in range(0, len(candidates), stage_size):
            if offset:
                wait_for_quiet(
                    quiet_seconds=max(0, args.quiet_seconds),
                    timeout_seconds=max(30, args.quiet_timeout_seconds),
                )
            stage = candidates[offset : offset + stage_size]
            job = submit_routed_job(
                [candidate.email for candidate in stage],
                settings.max_workers_per_job,
                owner_id=None,
                owner_email=None,
                list_name=SMTP_REVIEW_CANARY_LIST_NAME,
                is_cache_refresh=True,
            )
            root_ids.append(job.id)
            stage_reports.append(wait_for_stage(
                job.id,
                timeout_seconds=max(60, args.stage_timeout_seconds),
            ))

        reviews_settled = wait_for_reviews(
            root_ids,
            timeout_seconds=max(0, args.review_timeout_seconds),
        )
        elapsed = round((utc_now() - started_at).total_seconds(), 2)
        report = {
            "kind": "smtp_review_history_canary",
            "addresses": len(candidates),
            "cohorts": dict(sorted(Counter(item.cohort for item in candidates).items())),
            "source_targets": dict(sorted(Counter(item.source_target for item in candidates).items())),
            "unique_domains": len({_domain(item.email) for item in candidates}),
            "elapsed_seconds": elapsed,
            "reviews_settled": reviews_settled,
            "stages": [report.__dict__ for report in stage_reports],
            "result_matrix": _result_matrix(candidates, root_ids),
            "smtp_cross_route": _review_report(root_ids),
        }
        print(json.dumps(report, ensure_ascii=True, sort_keys=True))
        return 0 if reviews_settled and len(candidates) == total else 1
    finally:
        stopped = stop_unfinished_canary(started_at)
        if stopped:
            print(json.dumps({"canary_jobs_stopped": stopped}, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
