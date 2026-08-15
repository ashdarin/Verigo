"""Contract checks for the non-blocking quality-dashboard index rollout."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ensure_quality_dashboard_schema import (  # noqa: E402
    INITIAL_COMPLETION_BACKFILL,
    QUALITY_DASHBOARD_INDEXES,
    main,
)


class Cursor:
    def __init__(self) -> None:
        self.sql = ""
        self.sqls = []
        self.rowcount = -1

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql: str) -> None:
        self.sql = sql
        self.sqls.append(sql)
        self.rowcount = 0 if sql == INITIAL_COMPLETION_BACKFILL else -1


class Connection:
    def __init__(self) -> None:
        self.cursor_instance = Cursor()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self) -> Cursor:
        return self.cursor_instance


def run() -> int:
    connection = Connection()
    with patch("scripts.ensure_quality_dashboard_schema.resolve_database_url", return_value="postgresql://example"), patch(
        "scripts.ensure_quality_dashboard_schema.connect", return_value=connection
    ) as connect:
        assert main() == 0
    assert connect.call_args.kwargs["autocommit"] is True
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in connection.cursor_instance.sql
    assert any("ADD COLUMN IF NOT EXISTS initial_completed_at" in sql for sql in connection.cursor_instance.sqls)
    assert any("LEAST(result.updated_at, job.finished_at)" in sql for sql in connection.cursor_instance.sqls)
    assert len(QUALITY_DASHBOARD_INDEXES) == 3
    assert QUALITY_DASHBOARD_INDEXES[1][0] == "idx_job_results_initial_quality_window"
    assert QUALITY_DASHBOARD_INDEXES[-1][0] in connection.cursor_instance.sql
    print("quality dashboard schema smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
