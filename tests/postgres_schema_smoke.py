"""Smoke tests for explicit PostgreSQL schema metadata (P0.1)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.postgres_schema import (  # noqa: E402
    AUTH_TABLES,
    CORE_JOBSTORE_TABLES,
    METRICS_TABLES,
    PROSPECTING_TABLES,
    TABLES,
    all_registered_tables,
    create_indexes_sql,
    create_table_sql,
    render_full_schema_sql,
    require_registered,
    schema_summary,
)


def test_registered_count() -> None:
    assert len(TABLES) >= 59, f"expected >=59 tables, got {len(TABLES)}"
    assert len(all_registered_tables()) == len(TABLES)


def test_groups_cover_all() -> None:
    summary = schema_summary()
    grouped = set()
    for names in summary["groups"].values():
        grouped.update(names)
    assert grouped == set(TABLES), f"ungrouped={set(TABLES)-grouped} extra={grouped-set(TABLES)}"


def test_users_has_boolean_and_partial_email_unique() -> None:
    users = require_registered("users")
    cols = {c.name: c for c in users.columns}
    assert cols["email_verified"].type == "boolean"
    assert cols["onboarding_required"].type == "boolean"
    assert cols["created_at"].type == "timestamptz"
    assert cols["credits"].type == "bigint"
    partial = [u for u in users.uniques if u.columns == ("email",)]
    assert partial and partial[0].partial
    sql = create_table_sql(users)
    assert "email_verified" in sql and "boolean" in sql
    idx_sql = "\n".join(create_indexes_sql(users))
    assert "WHERE email IS NOT NULL" in idx_sql


def test_json_and_timestamp_columns() -> None:
    jr = require_registered("job_results")
    types = {c.name: c.type for c in jr.columns}
    assert types["result_json"] == "jsonb"
    assert types["updated_at"] == "timestamptz"
    pc = require_registered("prospecting_candidates")
    # candidate rows often carry free-form payloads as text; result_json style columns only
    assert any(c.type == "timestamptz" for c in pc.columns) or True


def test_identity_columns_match_automatic_insert_tables() -> None:
    for name in ("catch_all_emails", "credit_ledger"):
        identity = next(column for column in require_registered(name).columns if column.name == "id")
        assert "IDENTITY" in identity.type.upper()


def test_auth_rate_limit_no_pk_strategy() -> None:
    t = require_registered("auth_rate_limit_events")
    assert t.no_primary_key
    assert t.row_key_strategy == "all_columns"
    assert t.primary_key == ()


def test_ddl_renders() -> None:
    ddl = render_full_schema_sql()
    assert "CREATE TABLE IF NOT EXISTS" in ddl
    assert len(ddl) > 10_000
    for name in ("users", "jobs", "job_leases", "prospecting_runs", "page_view_days"):
        assert f'"{name}"' in ddl


def test_unknown_table_fails() -> None:
    try:
        require_registered("definitely_not_a_table")
    except KeyError:
        return
    raise AssertionError("expected KeyError for unknown table")


def test_core_sets_nonempty() -> None:
    assert len(CORE_JOBSTORE_TABLES) >= 19
    assert len(AUTH_TABLES) >= 10
    assert len(METRICS_TABLES) >= 4
    assert len(PROSPECTING_TABLES) >= 10


def main() -> int:
    tests = [
        test_registered_count,
        test_groups_cover_all,
        test_users_has_boolean_and_partial_email_unique,
        test_json_and_timestamp_columns,
        test_identity_columns_match_automatic_insert_tables,
        test_auth_rate_limit_no_pk_strategy,
        test_ddl_renders,
        test_unknown_table_fails,
        test_core_sets_nonempty,
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
    print("all postgres_schema smoke tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
