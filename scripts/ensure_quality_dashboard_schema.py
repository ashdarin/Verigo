"""Apply additive PostgreSQL indexes needed by the administrator quality view."""
from __future__ import annotations

from app.db.postgresql import connect, resolve_database_url


QUALITY_WINDOW_INDEX = "idx_job_results_quality_window"


def main() -> int:
    # CREATE INDEX CONCURRENTLY cannot run in a transaction.  A release can be
    # retried safely because PostgreSQL retains the completed index by name.
    with connect(resolve_database_url(), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
                f"{QUALITY_WINDOW_INDEX} ON job_results (updated_at, progress_state)"
            )
    print(f"ensured {QUALITY_WINDOW_INDEX}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
