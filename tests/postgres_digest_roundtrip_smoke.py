"""Digest stability for jobs-like rows across SQLite-ish and PG-native values."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.postgres_schema import require_registered  # noqa: E402
from app.db.postgres_shadow import row_digest  # noqa: E402


def test_jobs_row_digest_sqlite_vs_pg_natives() -> None:
    table = require_registered("jobs")
    sqlite_like = {
        "id": "j1",
        "emails_json": '["a@example.com","b@example.com"]',
        "worker_count": 2,
        "status": "completed",
        "created_at": "2026-01-02T03:04:05+00:00",
        "started_at": "2026-01-02 03:04:06",
        "finished_at": "2026-01-02T03:05:00.123456+00:00",
        "error": "",
        "results_json": '[{"email":"a@example.com","ok":true,"score":1.0}]',
        "csv_path": None,
        "owner_id": "u1",
        "guest_token_hash": None,
        "worker_id": "w1",
        "heartbeat_at": "2026-01-02T03:04:50Z",
        "stop_on_deliverable": 0,
        "execution_target": "local",
        "parent_id": None,
        "deferred_retry_at": None,
        "temporary_retry_attempts": 0,
        "retry_parent_id": None,
        "enrich_profiles": 0,
        "list_name": None,
    }
    pg_like = {
        "id": "j1",
        "emails_json": ["a@example.com", "b@example.com"],
        "worker_count": 2,
        "status": "completed",
        "created_at": datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        "started_at": datetime(2026, 1, 2, 3, 4, 6, tzinfo=timezone.utc),
        "finished_at": datetime(2026, 1, 2, 3, 5, 0, 123456, tzinfo=timezone.utc),
        "error": None,  # blank text treated as null in digest
        "results_json": [{"email": "a@example.com", "ok": True, "score": 1.0}],
        "csv_path": None,
        "owner_id": "u1",
        "guest_token_hash": None,
        "worker_id": "w1",
        "heartbeat_at": datetime(2026, 1, 2, 3, 4, 50, tzinfo=timezone.utc),
        "stop_on_deliverable": False,
        "execution_target": "local",
        "parent_id": None,
        "deferred_retry_at": None,
        "temporary_retry_attempts": 0,
        "retry_parent_id": None,
        "enrich_profiles": 0,
        "list_name": None,
    }
    d1 = row_digest(table, sqlite_like)
    d2 = row_digest(table, pg_like)
    assert d1 == d2, f"digest mismatch\n{d1}\n{d2}"


def main() -> int:
    try:
        test_jobs_row_digest_sqlite_vs_pg_natives()
        print("OK  test_jobs_row_digest_sqlite_vs_pg_natives")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL test_jobs_row_digest_sqlite_vs_pg_natives: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
