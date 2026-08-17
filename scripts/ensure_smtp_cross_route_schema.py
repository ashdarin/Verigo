"""Install additive metadata and immutable events for SMTP route reviews."""

from __future__ import annotations

from app.db.postgresql import connect, resolve_database_url


STATEMENTS = (
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS retry_route text NOT NULL DEFAULT 'same_target'",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS origin_execution_target text",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cross_route_attempts bigint NOT NULL DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS idx_jobs_cross_route_queue "
    "ON jobs(execution_target, retry_route, status, created_at)",
    """CREATE TABLE IF NOT EXISTS smtp_review_events(
        id text PRIMARY KEY,
        parent_job_id text NOT NULL,
        retry_job_id text,
        email_hash text NOT NULL,
        provider_key text NOT NULL,
        event_type text NOT NULL,
        decision_reason text,
        origin_execution_target text NOT NULL,
        review_execution_target text,
        retry_route text NOT NULL,
        attempt bigint NOT NULL DEFAULT 0,
        initial_smtp_code text,
        review_smtp_code text,
        outcome text,
        occurred_at timestamptz NOT NULL,
        initial_completed_at timestamptz,
        review_started_at timestamptz,
        review_completed_at timestamptz,
        latency_ms bigint
    )""",
    "CREATE INDEX IF NOT EXISTS idx_smtp_review_events_occurred "
    "ON smtp_review_events(occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_smtp_review_events_type_time "
    "ON smtp_review_events(event_type, occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_smtp_review_events_provider_time "
    "ON smtp_review_events(provider_key, occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_smtp_review_events_parent_email "
    "ON smtp_review_events(parent_job_id, email_hash, attempt)",
)


def main() -> int:
    with connect(resolve_database_url(), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '5s'")
            for statement in STATEMENTS:
                cursor.execute(statement)
    print("smtp cross-route schema and event log ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
