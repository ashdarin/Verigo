"""Create the additive PostgreSQL tables for privacy-preserving product metrics."""
from __future__ import annotations

from app.db.postgres_schema import create_table_sql, require_registered
from app.db.postgresql import connect, resolve_database_url


COMPANY_FINDER_METRICS_TABLES = (
    "company_finder_event_days",
    "company_finder_event_users",
)


def main() -> int:
    with connect(resolve_database_url(), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '5s'")
            for name in COMPANY_FINDER_METRICS_TABLES:
                cursor.execute(create_table_sql(require_registered(name)))
    print("ensured " + ", ".join(COMPANY_FINDER_METRICS_TABLES))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
