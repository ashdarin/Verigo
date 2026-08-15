"""Apply additive PostgreSQL indexes needed by the administrator quality view."""
from __future__ import annotations

from app.db.postgresql import connect, resolve_database_url


QUALITY_DASHBOARD_INDEXES = (
    ("idx_job_results_quality_window", "updated_at, progress_state"),
    ("idx_job_results_initial_quality_window", "initial_completed_at, progress_state"),
    ("idx_job_results_review_backlog", "retry_at"),
)

INITIAL_COMPLETION_BACKFILL = """
WITH batch AS (
    SELECT result.ctid AS row_ctid,
        COALESCE(LEAST(result.updated_at, job.finished_at), result.updated_at) AS completed_at
    FROM job_results AS result
    LEFT JOIN jobs AS job ON job.id=result.job_id
    WHERE result.initial_completed_at IS NULL
        AND result.progress_state IN ('completed', 'failed', 'stopped')
    LIMIT 10000
)
UPDATE job_results AS result
SET initial_completed_at=batch.completed_at
FROM batch
WHERE result.ctid=batch.row_ctid
"""


def main() -> int:
    # CREATE INDEX CONCURRENTLY cannot run in a transaction.  A release can be
    # retried safely because PostgreSQL retains the completed index by name.
    with connect(resolve_database_url(), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE job_results "
                "ADD COLUMN IF NOT EXISTS initial_completed_at timestamptz"
            )
            backfilled = 0
            while True:
                cursor.execute(INITIAL_COMPLETION_BACKFILL)
                changed = max(0, cursor.rowcount)
                backfilled += changed
                if changed == 0:
                    break
            for name, columns in QUALITY_DASHBOARD_INDEXES:
                cursor.execute(
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
                    f"{name} ON job_results ({columns})"
                )
    print(
        f"backfilled {backfilled} initial completion timestamps; ensured "
        + ", ".join(name for name, _columns in QUALITY_DASHBOARD_INDEXES)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
