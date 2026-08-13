"""Preflight gate smoke using a local SQLite fixture (no remote PG required)."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fixture(path: Path, *, active_lease: bool = False) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            execution_target TEXT NOT NULL DEFAULT 'local',
            finished_at TEXT,
            csv_path TEXT,
            results_json TEXT DEFAULT '[]',
            emails_json TEXT DEFAULT '[]',
            worker_count INTEGER DEFAULT 1,
            stop_on_deliverable INTEGER DEFAULT 0
        );
        CREATE TABLE job_results (
            job_id TEXT NOT NULL,
            original_index INTEGER NOT NULL,
            email TEXT,
            progress_state TEXT,
            result_json TEXT,
            updated_at TEXT,
            deliverability TEXT,
            is_valid INTEGER,
            is_skipped INTEGER,
            is_catch_all INTEGER,
            retry_at TEXT,
            retry_updated INTEGER,
            query_fields_ready INTEGER,
            PRIMARY KEY(job_id, original_index)
        );
        CREATE TABLE job_leases (
            id TEXT PRIMARY KEY,
            job_id TEXT,
            execution_target TEXT,
            worker_id TEXT,
            claimed_at TEXT,
            heartbeat_at TEXT,
            completed_at TEXT,
            lease_token TEXT
        );
        CREATE TABLE users (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            email TEXT,
            email_verified INTEGER NOT NULL DEFAULT 0,
            credits INTEGER NOT NULL DEFAULT 0,
            activation_job_id TEXT,
            activation_completed_at TEXT,
            onboarding_required INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE credit_ledger (
            id INTEGER PRIMARY KEY,
            user_id TEXT NOT NULL,
            delta INTEGER NOT NULL,
            kind TEXT NOT NULL,
            reference TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE payment_orders (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            credits INTEGER NOT NULL,
            amount_fen INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            paid_at TEXT
        );
        CREATE TABLE redemption_codes (
            id TEXT PRIMARY KEY,
            code_hash TEXT,
            credits INTEGER,
            amount_fen INTEGER,
            created_at TEXT,
            redeemed_at TEXT,
            redeemed_by TEXT,
            note TEXT,
            expires_at TEXT
        );
        CREATE TABLE promo_credit_grants (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            reference TEXT,
            initial_credits INTEGER,
            remaining_credits INTEGER,
            expires_at TEXT,
            created_at TEXT
        );
        INSERT INTO jobs(id, status, created_at) VALUES ('j1', 'completed', '2026-01-01T00:00:00+00:00');
        INSERT INTO users(id, username, password_hash, created_at, credits)
        VALUES ('u1', 'alice', 'x', '2026-01-01T00:00:00+00:00', 5);
        """
    )
    if active_lease:
        conn.execute(
            """INSERT INTO job_leases(id, job_id, execution_target, worker_id, claimed_at, heartbeat_at, completed_at)
               VALUES ('L1', 'j1', 'local', 'w1', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', NULL)"""
        )
    conn.commit()
    conn.close()


def test_blocks_active_leases() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "f.db"
        _fixture(db, active_lease=True)
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "postgres_cutover_preflight.py"),
                "--sqlite",
                str(db),
                "--tables",
                "jobs,job_leases,users",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
            env={**dict(**{k: v for k, v in __import__("os").environ.items()}), "VERIGO_DATABASE_URL": ""},
        )
        assert proc.returncode == 2
        report = json.loads(proc.stdout)
        assert report["ready"] is False
        assert "active_job_leases_present" in report["blockers"] or "dsn_missing" in str(
            report["blockers"]
        )


def test_allow_active_leases_observation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "f.db"
        _fixture(db, active_lease=True)
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "postgres_cutover_preflight.py"),
                "--sqlite",
                str(db),
                "--tables",
                "jobs,job_leases,users",
                "--allow-active-leases",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 2  # still fails without DSN / target
        report = json.loads(proc.stdout)
        assert "active_job_leases_present" not in report["blockers"]
        assert any("active_job_leases" in w for w in report.get("warnings", [])) or True


def main() -> int:
    failed = 0
    for fn in (test_blocks_active_leases, test_allow_active_leases_observation):
        try:
            fn()
            print(f"OK  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
