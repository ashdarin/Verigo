"""Upgrade and backfill the confidence-aware verification result cache."""

from __future__ import annotations

from app.db.postgres_schema import create_table_sql, require_registered
from app.db.postgresql import connect, resolve_database_url


NEW_TABLES = (
    "verification_probe_leases",
    "verification_probe_waiters",
    "verification_cache_days",
)
MIGRATION_KEY = "verification_cache_schema_version"
MIGRATION_VERSION = "1"

ADDITIVE_COLUMNS = (
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS is_cache_refresh boolean NOT NULL DEFAULT false",
    "ALTER TABLE verification_cache ADD COLUMN IF NOT EXISTS outcome_class text NOT NULL DEFAULT 'legacy'",
    "ALTER TABLE verification_cache ADD COLUMN IF NOT EXISTS verified_at timestamptz",
    "ALTER TABLE verification_cache ADD COLUMN IF NOT EXISTS stale_expires_at timestamptz",
    "ALTER TABLE verification_cache ADD COLUMN IF NOT EXISTS hit_count bigint NOT NULL DEFAULT 0",
    "ALTER TABLE verification_cache ADD COLUMN IF NOT EXISTS last_hit_at timestamptz",
    "ALTER TABLE verification_cache ADD COLUMN IF NOT EXISTS refresh_requested_at timestamptz",
    "ALTER TABLE verified_emails ADD COLUMN IF NOT EXISTS confirmation_count bigint NOT NULL DEFAULT 1",
)

INDEXES = (
    ("idx_verification_cache_stale", "verification_cache", "stale_expires_at"),
    ("idx_verification_cache_refresh", "verification_cache", "expires_at, hit_count"),
    ("idx_verification_probe_leases_expiry", "verification_probe_leases", "expires_at"),
    ("idx_verification_probe_waiters_email", "verification_probe_waiters", "email"),
    ("idx_verification_probe_waiters_expiry", "verification_probe_waiters", "expires_at"),
)

BACKFILL_EXISTING_CACHE = """
UPDATE verification_cache
SET outcome_class=CASE
        WHEN result_json->>'domain_type'='catch-all'
             OR result_json->>'is_catch_all'='true'
             OR result_json->>'failure_reason'='catch_all_conflict'
            THEN 'legacy'
        WHEN result_json->>'deliverable'='true' THEN 'deliverable'
        WHEN result_json->>'failure_reason'='mailbox_full'
             OR result_json->>'delivery_block_reason'='mailbox_full'
            THEN 'mailbox_full'
        WHEN result_json->>'deliverable'='false' AND (
            result_json->>'failure_reason'='smtp_permanent'
            OR result_json->>'smtp_code' LIKE '5%'
            OR lower(COALESCE(result_json->>'verification_method', ''))
                IN ('microsoft_api', 'outlook 账号验证')
            OR lower(COALESCE(result_json->>'strategy', ''))='outlook_http'
        ) THEN 'permanent_invalid'
        ELSE 'legacy' END,
    verified_at=COALESCE(verified_at, updated_at),
    stale_expires_at=COALESCE(stale_expires_at, updated_at + INTERVAL '90 days')
WHERE verified_at IS NULL OR stale_expires_at IS NULL OR outcome_class='legacy'
"""

BACKFILL_CONFIRMATION_COUNTS = """
UPDATE verified_emails
SET confirmation_count=GREATEST(
    confirmation_count,
    CASE WHEN last_confirmed_at > first_confirmed_at THEN 2 ELSE 1 END
)
"""

BACKFILL_VERIFIED_EMAILS = """
WITH latest AS (
    SELECT DISTINCT ON (lower(email))
        lower(email) AS email, updated_at, result_json
    FROM job_results
    WHERE progress_state='completed' AND deliverability=1 AND is_catch_all IS NOT TRUE
        AND COALESCE(result_json->>'cache_hit', 'false') <> 'true'
    ORDER BY lower(email), updated_at DESC
)
INSERT INTO verified_emails(
    email, first_confirmed_at, last_confirmed_at, result_json, confirmation_count
)
SELECT email, updated_at, updated_at, result_json, 1 FROM latest
ON CONFLICT(email) DO UPDATE SET
    last_confirmed_at=EXCLUDED.last_confirmed_at,
    result_json=EXCLUDED.result_json,
    confirmation_count=verified_emails.confirmation_count + 1
WHERE EXCLUDED.last_confirmed_at > verified_emails.last_confirmed_at
"""

BACKFILL_DELIVERABLE_CACHE = """
INSERT INTO verification_cache(
    email, result_json, expires_at, updated_at, outcome_class, verified_at,
    stale_expires_at, hit_count
)
SELECT email, result_json,
    last_confirmed_at + CASE
        WHEN confirmation_count >= 2
             AND last_confirmed_at - first_confirmed_at >= INTERVAL '7 days'
            THEN INTERVAL '30 days'
        WHEN confirmation_count >= 2
             AND last_confirmed_at - first_confirmed_at >= INTERVAL '1 day'
            THEN INTERVAL '14 days'
        ELSE INTERVAL '7 days' END,
    last_confirmed_at, 'deliverable', last_confirmed_at,
    last_confirmed_at + INTERVAL '90 days', 0
FROM verified_emails
WHERE COALESCE(result_json->>'domain_type', '') <> 'catch-all'
    AND COALESCE(result_json->>'is_catch_all', 'false') <> 'true'
    AND COALESCE(result_json->>'failure_reason', '') <> 'catch_all_conflict'
ON CONFLICT(email) DO UPDATE SET
    result_json=EXCLUDED.result_json, expires_at=EXCLUDED.expires_at,
    updated_at=EXCLUDED.updated_at, outcome_class=EXCLUDED.outcome_class,
    verified_at=EXCLUDED.verified_at, stale_expires_at=EXCLUDED.stale_expires_at
WHERE EXCLUDED.updated_at > verification_cache.updated_at
"""

BACKFILL_NEGATIVE_CACHE = """
WITH latest AS (
    SELECT DISTINCT ON (lower(email))
        lower(email) AS email, updated_at, result_json, deliverability, is_catch_all
    FROM job_results
    WHERE progress_state='completed'
    ORDER BY lower(email), updated_at DESC
), classified AS (
    SELECT *, CASE
        WHEN result_json->>'failure_reason'='mailbox_full' THEN 'mailbox_full'
        WHEN deliverability=0 AND result_json->>'failure_reason'='smtp_permanent'
            THEN 'permanent_invalid'
        ELSE NULL END AS outcome_class
    FROM latest WHERE is_catch_all IS NOT TRUE
        AND COALESCE(result_json->>'cache_hit', 'false') <> 'true'
)
INSERT INTO verification_cache(
    email, result_json, expires_at, updated_at, outcome_class, verified_at,
    stale_expires_at, hit_count
)
SELECT email, result_json,
    updated_at + CASE WHEN outcome_class='mailbox_full'
        THEN INTERVAL '2 hours' ELSE INTERVAL '3 days' END,
    updated_at, outcome_class, updated_at,
    updated_at + CASE WHEN outcome_class='mailbox_full'
        THEN INTERVAL '1 day' ELSE INTERVAL '90 days' END,
    0
FROM classified WHERE outcome_class IS NOT NULL
ON CONFLICT(email) DO UPDATE SET
    result_json=EXCLUDED.result_json, expires_at=EXCLUDED.expires_at,
    updated_at=EXCLUDED.updated_at, outcome_class=EXCLUDED.outcome_class,
    verified_at=EXCLUDED.verified_at, stale_expires_at=EXCLUDED.stale_expires_at
WHERE EXCLUDED.updated_at > verification_cache.updated_at
"""


def main() -> int:
    counts: dict[str, int] = {}
    with connect(resolve_database_url(), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET statement_timeout = 0")
            cursor.execute("SET lock_timeout = '5s'")
            for statement in ADDITIVE_COLUMNS:
                cursor.execute(statement)
            for table in NEW_TABLES:
                cursor.execute(create_table_sql(require_registered(table)))
            for name, table, columns in INDEXES:
                cursor.execute(f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" ({columns})')
            cursor.execute("SELECT value FROM service_state WHERE name=%s", (MIGRATION_KEY,))
            row = cursor.fetchone()
            current_version = (
                row.get("value") if isinstance(row, dict)
                else row[0] if row is not None else None
            )
            if str(current_version or "") != MIGRATION_VERSION:
                for name, statement in (
                    ("existing_cache", BACKFILL_EXISTING_CACHE),
                    ("confirmation_counts", BACKFILL_CONFIRMATION_COUNTS),
                    ("verified_emails", BACKFILL_VERIFIED_EMAILS),
                    ("deliverable_cache", BACKFILL_DELIVERABLE_CACHE),
                    ("negative_cache", BACKFILL_NEGATIVE_CACHE),
                ):
                    cursor.execute(statement)
                    counts[name] = max(0, cursor.rowcount)
                cursor.execute("""
                    INSERT INTO service_state(name, value, updated_at) VALUES (%s, %s, NOW())
                    ON CONFLICT(name) DO UPDATE SET
                        value=EXCLUDED.value, updated_at=EXCLUDED.updated_at
                """, (MIGRATION_KEY, MIGRATION_VERSION))
    print("verification cache schema ready; " + ", ".join(
        f"{name}={count}" for name, count in counts.items()
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
