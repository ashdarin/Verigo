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
        self.queries = []

    def execute(self, sql, params=()):
        self.sql = sql
        self.params = params
        self.queries.append(sql)
        return self

    def fetchall(self):
        return [
            ("gmail", 10, 7, 2, 4, 3, 2, 1, 3, 0, 8, 9.6, 20.4, 3, 900, 3600),
            ("qq", 2, 1, 1, 0, 0, 0, 1, 0, 2, 2, 12.2, 18.9, 0, 0, 0),
        ]

    def fetchone(self):
        return (3,)


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
    assert rows["gmail"]["latency_sample"] == 8
    assert rows["gmail"]["p50_seconds"] == 10 and rows["gmail"]["p95_seconds"] == 20
    assert rows["gmail"]["review_latency_sample"] == 3
    assert rows["gmail"]["review_p50_seconds"] == 900
    assert rows["gmail"]["review_p95_seconds"] == 3600
    assert rows["microsoft"]["deliverable_rate"] is None
    assert quality["total"] == 12 and quality["deliverable"] == 8 and quality["unknown"] == 3
    assert quality["reviewed"] == 3
    assert quality["review_backlog"] == 3
    assert quality["risk_flags"] == {"disposable": 2, "mailbox_full": 2, "role_address": 3, "do_not_reply": 2}
    assert any("result.updated_at >= ?" in query for query in connection.queries)
    assert any("result.initial_completed_at" in query for query in connection.queries)
    assert any("result.updated_at - result.initial_completed_at" in query for query in connection.queries)
    assert not any("job.finished_at AS initial_completed_at" in query for query in connection.queries)
    assert any("job.parent_id IS NULL" in query for query in connection.queries)
    assert any("PERCENTILE_CONT" in query for query in connection.queries)
    assert any("result.retry_at IS NOT NULL" in query for query in connection.queries)
    assert any("child.status IN ('queued', 'running')" in query for query in connection.queries)
    assert "baseline" not in quality
    print("provider quality smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
