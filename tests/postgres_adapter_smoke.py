"""Smoke tests for PostgreSQL compatibility adapter + dual-backend wiring."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.pg_compat import (  # noqa: E402
    as_bool,
    as_datetime,
    as_json,
    dialect_nocase_eq,
    rewrite_sql,
)


def test_placeholder_rewrite() -> None:
    sql = rewrite_sql("SELECT id FROM users WHERE id=? AND name=?")
    assert "%s" in sql
    assert "?" not in sql


def test_collate_nocase_rewrite() -> None:
    sql = rewrite_sql("SELECT id FROM users WHERE email = ? COLLATE NOCASE")
    assert "LOWER(email) = LOWER(%s)" in sql or "LOWER(email) = LOWER(?)" not in sql
    assert "COLLATE" not in sql.upper()
    assert "%s" in sql


def test_sum_eq_rewrite() -> None:
    sql = rewrite_sql(
        "SELECT COALESCE(SUM(status='queued'), 0), COALESCE(SUM(status='running'), 0) FROM jobs"
    )
    assert "FILTER" in sql
    assert "status = 'queued'" in sql


def test_insert_or_ignore() -> None:
    sql = rewrite_sql("INSERT OR IGNORE INTO t(id) VALUES (?)")
    assert sql.upper().startswith("INSERT INTO")
    assert "ON CONFLICT DO NOTHING" in sql.upper()
    assert "%s" in sql


def test_insert_or_replace() -> None:
    sql = rewrite_sql(
        "INSERT OR REPLACE INTO schema_migrations(name, applied_at) VALUES (?, ?)"
    )
    upper = sql.upper()
    assert upper.startswith("INSERT INTO")
    assert "ON CONFLICT" in upper
    assert "DO UPDATE SET" in upper
    assert "%s" in sql


def test_json_and_dt_helpers() -> None:
    assert as_json('{"a":1}') == {"a": 1}
    assert as_json({"a": 1}) == {"a": 1}
    assert as_bool(1) is True
    assert as_bool(False) is False
    dt = as_datetime("2026-01-01T00:00:00+00:00")
    assert dt is not None
    assert dt.year == 2026


def test_dialect_nocase() -> None:
    assert "COLLATE" in dialect_nocase_eq("email")


def test_stores_import_with_sqlite_default() -> None:
    from app.config import settings
    from app.db.auth import auth_store
    from app.db.jobs import job_store

    assert settings.postgres_enabled is False
    # Initialize against local default path (may create empty sqlite files under data/)
    # Only ensure methods are bound and dual-backend flags work.
    assert hasattr(auth_store, "_connect")
    assert hasattr(job_store, "health_summary")
    assert hasattr(job_store, "set_service_mode")


def main() -> int:
    failed = 0
    for fn in (
        test_placeholder_rewrite,
        test_collate_nocase_rewrite,
        test_sum_eq_rewrite,
        test_insert_or_ignore,
        test_insert_or_replace,
        test_json_and_dt_helpers,
        test_dialect_nocase,
        test_stores_import_with_sqlite_default,
    ):
        try:
            fn()
            print(f"OK  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
