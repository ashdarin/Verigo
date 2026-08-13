"""Local SQLite fixture smoke for migration digests and dry-run path."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.postgres_migrate import (  # noqa: E402
    fetch_sqlite_rows,
    normalize_rows,
    open_sqlite,
    summarize_table,
)
from app.db.postgres_schema import require_registered  # noqa: E402
from app.db.postgres_shadow import (  # noqa: E402
    coerce_sqlite_value,
    normalize_for_digest,
    row_digest,
)


def _build_fixture(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            email TEXT,
            email_verified INTEGER NOT NULL DEFAULT 0,
            credits INTEGER NOT NULL DEFAULT 0,
            activation_job_id TEXT,
            activation_completed_at TEXT,
            onboarding_required INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE service_state (
            name TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        -- Intentionally incomplete vs postgres_schema jobs: missing columns with
        -- NOT NULL DEFAULT must still digest/migrate as their schema defaults.
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            emails_json TEXT NOT NULL DEFAULT '[]',
            worker_count INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            error TEXT,
            results_json TEXT NOT NULL DEFAULT '[]',
            csv_path TEXT,
            owner_id TEXT,
            guest_token_hash TEXT,
            worker_id TEXT,
            heartbeat_at TEXT,
            progress_done INTEGER NOT NULL DEFAULT 0,
            progress_total INTEGER NOT NULL DEFAULT 0,
            parent_job_id TEXT,
            shard_index INTEGER,
            shard_count INTEGER,
            priority INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO users(id, username, password_hash, created_at, email, email_verified, credits)
        VALUES ('u1', 'alice', 'hash', '2026-01-01T00:00:00+00:00', 'a@example.com', 1, 10);
        INSERT INTO service_state(name, value, updated_at)
        VALUES ('verification_mode', 'active', '2026-01-01T00:00:00+00:00');
        INSERT INTO jobs(id, status, created_at, emails_json, results_json, started_at, finished_at, worker_id)
        VALUES (
            'j1',
            'completed',
            '2026-01-01T00:00:00.123456+00:00',
            '["a@example.com"]',
            '[{"email":"a@example.com","valid":true,"score":1.0,"checks":{"format":true,"z":1,"a":2}}]',
            '2026-01-01T00:00:01.999999+00:00',
            '',
            ''
        );
        """
    )
    conn.commit()
    conn.close()


def test_coerce_and_digest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "fixture.db"
        _build_fixture(db)
        conn = open_sqlite(db)
        try:
            users = require_registered("users")
            rows = normalize_rows(users, fetch_sqlite_rows(conn, users))
            assert rows[0]["email_verified"] is True
            assert rows[0]["credits"] == 10
            summary = summarize_table(users, rows)
            assert summary["count"] == 1
            assert len(summary["content_digest"]) == 64
        finally:
            conn.close()


def test_bool_parse() -> None:
    col = require_registered("users").columns
    email_verified = next(c for c in col if c.name == "email_verified")
    assert coerce_sqlite_value(email_verified, 1) is True
    assert coerce_sqlite_value(email_verified, 0) is False


def test_jobs_content_digest_matches_pg_shaped_row() -> None:
    """SQLite incomplete jobs rows must match PG-native rows after defaults/json/ts normalize."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "fixture.db"
        _build_fixture(db)
        conn = open_sqlite(db)
        try:
            jobs = require_registered("jobs")
            sqlite_rows = normalize_rows(jobs, fetch_sqlite_rows(conn, jobs))
            assert len(sqlite_rows) == 1
            row = sqlite_rows[0]

            # Missing NOT NULL columns filled from schema defaults.
            assert row["stop_on_deliverable"] is False
            assert row["execution_target"] == "local"
            assert row["temporary_retry_attempts"] == 0
            assert row["enrich_profiles"] == 0
            # Empty timestamp / blank text coerced for stability.
            assert row["finished_at"] is None
            assert row["worker_id"] == ""
            assert row["list_name"] is None

            # JSON: nested key order + integer-valued float collapse.
            results = row["results_json"]
            assert isinstance(results, list)
            assert results[0]["score"] == 1.0 or results[0]["score"] == 1

            pg_shaped = {
                "id": "j1",
                "emails_json": ["a@example.com"],
                "worker_count": 1,
                "status": "completed",
                "created_at": datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=timezone.utc),
                "started_at": datetime(2026, 1, 1, 0, 0, 1, 999999, tzinfo=timezone.utc),
                "finished_at": None,
                "error": None,
                "results_json": [
                    {
                        "email": "a@example.com",
                        "valid": True,
                        "score": 1,  # PG jsonb often returns 1.0 as int
                        "checks": {"a": 2, "z": 1, "format": True},
                    }
                ],
                "csv_path": None,
                "owner_id": None,
                "guest_token_hash": None,
                "worker_id": None,  # blank vs null
                "heartbeat_at": None,
                "stop_on_deliverable": False,
                "execution_target": "local",
                "parent_id": None,
                "deferred_retry_at": None,
                "temporary_retry_attempts": 0,
                "retry_parent_id": None,
                "enrich_profiles": 0,
                "list_name": None,
            }

            assert row_digest(jobs, row) == row_digest(jobs, pg_shaped)

            created = next(c for c in jobs.columns if c.name == "created_at")
            assert normalize_for_digest(created, row["created_at"]) == "2026-01-01T00:00:00Z"
            results_col = next(c for c in jobs.columns if c.name == "results_json")
            assert normalize_for_digest(results_col, row["results_json"]) == normalize_for_digest(
                results_col, pg_shaped["results_json"]
            )
            assert '"score":1' in normalize_for_digest(results_col, row["results_json"])
        finally:
            conn.close()


def test_json_canonical_float_and_key_order() -> None:
    jobs = require_registered("jobs")
    col = next(c for c in jobs.columns if c.name == "results_json")
    left = [{"b": 1, "a": 2.0, "nested": {"z": 1, "a": 1.0}}]
    right = json.dumps([{"a": 2, "b": 1, "nested": {"a": 1, "z": 1}}])
    assert normalize_for_digest(col, left) == normalize_for_digest(col, right)


def test_dry_run_cli() -> None:
    import subprocess

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "fixture.db"
        _build_fixture(db)
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "migrate_sqlite_to_postgres.py"),
                "--sqlite",
                str(db),
                "--tables",
                "users,service_state,jobs",
                "--dry-run",
                "--allow-unknown-source-tables",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert '"phase": "verify"' in proc.stdout or '"phase":"verify"' in proc.stdout
        assert '"ok": true' in proc.stdout or '"ok":true' in proc.stdout


def main() -> int:
    failed = 0
    for fn in (
        test_coerce_and_digest,
        test_bool_parse,
        test_jobs_content_digest_matches_pg_shaped_row,
        test_json_canonical_float_and_key_order,
        test_dry_run_cli,
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
