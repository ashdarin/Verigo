"""Apply additive PostgreSQL indexes needed by the administrator quality view."""
from __future__ import annotations

from app.db.postgresql import connect, resolve_database_url


QUALITY_DASHBOARD_INDEXES = (
    ("idx_job_results_quality_window", "updated_at, progress_state"),
    ("idx_job_results_review_backlog", "retry_at"),
)


def main() -> int:
    # CREATE INDEX CONCURRENTLY cannot run in a transaction.  A release can be
    # retried safely because PostgreSQL retains the completed index by name.
    with connect(resolve_database_url(), autocommit=True) as connection:
        with connection.cursor() as cursor:
            for name, columns in QUALITY_DASHBOARD_INDEXES:
                cursor.execute(
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
                    f"{name} ON job_results ({columns})"
                )
    print("ensured " + ", ".join(name for name, _columns in QUALITY_DASHBOARD_INDEXES))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
