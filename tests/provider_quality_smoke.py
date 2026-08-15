"""Static contract checks for the administrator-only provider quality dashboard."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.metrics import MetricsStore  # noqa: E402


class FakeConnection:
    def __init__(self) -> None:
        self.sql = ""
        self.params = ()

    def execute(self, sql, params):
        self.sql = sql
        self.params = params
        return self

    def fetchall(self):
        return [
            ("gmail", 10, 7, 2, 4, 3, 2, 1, 3, 0, 9.6, 20.4),
            ("qq", 2, 1, 1, 0, 0, 0, 1, 0, 2, 12.2, 18.9),
        ]


def main() -> int:
    connection = FakeConnection()
    with patch("app.db.metrics.postgres_active", return_value=True):
        quality = MetricsStore._provider_quality(connection, "2026-08-15T00:00:00+00:00")
    rows = {item["provider"]: item for item in quality["providers"]}
    assert quality["window_hours"] == 24
    assert tuple(rows) == ("gmail", "microsoft", "qq", "other")
    assert rows["gmail"]["deliverable_rate"] == 70.0
    assert rows["gmail"]["unconfirmed_rate"] == 20.0
    assert rows["gmail"]["review_completion_rate"] == 75.0
    assert rows["gmail"]["p50_seconds"] == 10 and rows["gmail"]["p95_seconds"] == 20
    assert rows["microsoft"]["deliverable_rate"] is None
    assert quality["total"] == 12 and quality["deliverable"] == 8 and quality["unknown"] == 3
    assert quality["reviewed"] == 3
    assert quality["risk_flags"] == {"disposable": 2, "mailbox_full": 2, "role_address": 3, "do_not_reply": 2}
    assert "result.updated_at >= ?" in connection.sql
    assert "job.parent_id IS NULL" in connection.sql
    assert "PERCENTILE_CONT" in connection.sql
    print("provider quality smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
