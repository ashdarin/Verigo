"""Verify that the metrics schema helper is additive and retryable."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ensure_company_finder_metrics_schema import (  # noqa: E402
    COMPANY_FINDER_METRICS_TABLES,
    main,
)


class Cursor:
    def __init__(self) -> None:
        self.sqls: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql: str) -> None:
        self.sqls.append(sql)


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
    with patch(
        "scripts.ensure_company_finder_metrics_schema.resolve_database_url",
        return_value="postgresql://example",
    ), patch(
        "scripts.ensure_company_finder_metrics_schema.connect", return_value=connection,
    ) as connect:
        assert main() == 0
    assert connect.call_args.kwargs["autocommit"] is True
    assert connection.cursor_instance.sqls[0] == "SET lock_timeout = '5s'"
    ddl = "\n".join(connection.cursor_instance.sqls[1:])
    assert len(COMPANY_FINDER_METRICS_TABLES) == 2
    assert all(f'"{name}"' in ddl for name in COMPANY_FINDER_METRICS_TABLES)
    assert "company_id" not in ddl and "domain" not in ddl and "email" not in ddl
    print("company finder metrics schema smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
