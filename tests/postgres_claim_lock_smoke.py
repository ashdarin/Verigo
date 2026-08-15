from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.db.jobs as jobs_module
from app.db.jobs import JobStore


class Cursor:
    def fetchone(self):
        return (False,)


class PgConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.rolled_back = False
        self.closed = False

    def execute(self, statement: str, params=None):
        normalized = " ".join(statement.split())
        self.statements.append(normalized)
        if normalized == "BEGIN":
            return Cursor()
        if normalized.startswith("SELECT pg_try_advisory_xact_lock"):
            assert params == ("tencent_qq", "same-worker")
            return Cursor()
        raise AssertionError(f"claim continued after advisory lock miss: {normalized}")

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


connection = PgConnection()
store = JobStore()
store._initialized = True
store._connect = lambda: connection  # type: ignore[method-assign]

with patch.object(jobs_module, "postgres_active", return_value=True):
    claimed = store.claim_remote_lease("same-worker", "tencent_qq")

assert claimed is None
assert connection.rolled_back is True
assert connection.closed is True
assert connection.statements == [
    "BEGIN",
    "SELECT pg_try_advisory_xact_lock(hashtext(?), hashtext(?))",
]

print("postgres claim lock smoke: ok")
