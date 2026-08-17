"""Produce bounded queue-health alerts for the operational monitor.

The script intentionally reads aggregate state only. It never selects email
addresses, job payloads, result objects, or database connection details.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.postgresql import connection, resolve_database_url


@dataclass(frozen=True)
class QueueHealth:
    oldest_queued_seconds: int | None = None
    running_without_lease: int = 0
    oldest_running_without_lease_seconds: int | None = None
    stale_workers: int = 0
    long_cooldowns: tuple[tuple[str, int], ...] = ()


def _seconds(value: Any) -> int | None:
    if value is None:
        return None
    return max(0, int(float(value)))


def read_health(
    *,
    queue_oldest_seconds: int,
    running_without_lease_seconds: int,
    worker_heartbeat_seconds: int,
    provider_cooldown_max_seconds: int,
) -> QueueHealth:
    """Read a small set of operational aggregates from PostgreSQL."""
    with connection(resolve_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXTRACT(EPOCH FROM CURRENT_TIMESTAMP - MIN(created_at)) AS seconds
                FROM jobs
                WHERE status = 'queued'
                  AND parent_id IS NULL
                  AND retry_parent_id IS NULL
                  AND is_cache_refresh IS NOT TRUE
                  AND created_at < CURRENT_TIMESTAMP - (%s * INTERVAL '1 second')
                """,
                (queue_oldest_seconds,),
            )
            oldest_queued_seconds = _seconds(cur.fetchone()["seconds"])

            cur.execute(
                """
                SELECT COUNT(*) AS jobs,
                       EXTRACT(
                           EPOCH FROM CURRENT_TIMESTAMP
                           - MIN(COALESCE(heartbeat_at, started_at, created_at))
                       ) AS oldest_seconds
                FROM jobs AS job
                WHERE job.status = 'running'
                  AND job.execution_target <> 'aggregate'
                  AND job.is_cache_refresh IS NOT TRUE
                  AND COALESCE(job.heartbeat_at, job.started_at, job.created_at)
                      < CURRENT_TIMESTAMP - (%s * INTERVAL '1 second')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM job_leases AS lease
                      WHERE lease.job_id = job.id
                        AND lease.completed_at IS NULL
                  )
                """,
                (running_without_lease_seconds,),
            )
            running = cur.fetchone()

            # worker_nodes identifies enabled nodes. Historical heartbeats for
            # a retired account must not create a permanent alert.
            cur.execute(
                """
                SELECT COUNT(*) AS workers
                FROM worker_heartbeats AS heartbeat
                JOIN worker_nodes AS node
                  ON node.target = heartbeat.target
                 AND node.worker_id = heartbeat.worker_id
                WHERE node.health = 'healthy'
                  AND heartbeat.last_seen_at
                      < CURRENT_TIMESTAMP - (%s * INTERVAL '1 second')
                """,
                (worker_heartbeat_seconds,),
            )
            stale_workers = int(cur.fetchone()["workers"])

            cur.execute(
                """
                SELECT scheduler_key,
                       EXTRACT(EPOCH FROM cooldown_until - CURRENT_TIMESTAMP) AS seconds
                FROM scheduler_domain_profiles
                WHERE cooldown_until > CURRENT_TIMESTAMP + (%s * INTERVAL '1 second')
                ORDER BY cooldown_until DESC
                LIMIT 8
                """,
                (provider_cooldown_max_seconds,),
            )
            long_cooldowns = tuple(
                (str(row["scheduler_key"]), _seconds(row["seconds"]) or 0)
                for row in cur.fetchall()
            )

    return QueueHealth(
        oldest_queued_seconds=oldest_queued_seconds,
        running_without_lease=int(running["jobs"]),
        oldest_running_without_lease_seconds=_seconds(running["oldest_seconds"]),
        stale_workers=stale_workers,
        long_cooldowns=long_cooldowns,
    )


def evaluate(health: QueueHealth) -> list[str]:
    alerts: list[str] = []
    if health.oldest_queued_seconds is not None:
        alerts.append(f"oldest queued job={health.oldest_queued_seconds}s")
    if health.running_without_lease:
        oldest = health.oldest_running_without_lease_seconds or 0
        alerts.append(
            f"running without active lease={health.running_without_lease}/{oldest}s"
        )
    if health.stale_workers:
        alerts.append(f"stale worker heartbeats={health.stale_workers}")
    if health.long_cooldowns:
        values = ", ".join(
            f"{scheduler_key}:{seconds}s"
            for scheduler_key, seconds in health.long_cooldowns
        )
        alerts.append(f"provider cooldown exceeds normal window: {values}")
    return alerts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue-oldest-seconds", type=int, default=900)
    parser.add_argument("--running-without-lease-seconds", type=int, default=300)
    parser.add_argument("--worker-heartbeat-seconds", type=int, default=300)
    parser.add_argument("--provider-cooldown-max-seconds", type=int, default=1800)
    args = parser.parse_args()
    health = read_health(
        queue_oldest_seconds=max(1, args.queue_oldest_seconds),
        running_without_lease_seconds=max(1, args.running_without_lease_seconds),
        worker_heartbeat_seconds=max(1, args.worker_heartbeat_seconds),
        provider_cooldown_max_seconds=max(1, args.provider_cooldown_max_seconds),
    )
    print("; ".join(evaluate(health)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
