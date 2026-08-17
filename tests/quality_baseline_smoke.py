"""Contract checks for the administrator-only seven-day quality baseline."""
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

    def execute(self, sql, params=()):
        self.sql = sql
        self.params = params
        return self

    def fetchall(self):
        return [
            ("2026-08-08", "gmail", 100, 10, 100, 100),
            ("2026-08-09", "gmail", 100, 12, 100, 120),
            ("2026-08-10", "gmail", 100, 8, 100, 80),
            ("2026-08-11", "gmail", 100, 9, 100, 110),
            ("2026-08-12", "gmail", 100, 11, 100, 90),
            ("2026-08-13", "gmail", 100, 10, 100, 100),
            ("2026-08-14", "gmail", 100, 10, 100, 100),
            ("2026-08-13", "microsoft", 49, 49, 49, 999),
            ("2026-08-14", "qq", 80, 4, 80, 60),
        ]


def main() -> int:
    connection = FakeConnection()
    with patch("app.db.metrics.postgres_active", return_value=True):
        baseline = MetricsStore._provider_quality_baseline(
            connection,
            "2026-08-08T00:00:00+00:00",
            "2026-08-15T00:00:00+00:00",
        )

    providers = {item["provider"]: item for item in baseline["providers"]}
    gmail = providers["gmail"]
    assert baseline["window_days"] == 7
    assert baseline["minimum_daily_sample"] == 50
    assert gmail["usable_days"] == 7 and gmail["ready"] is True
    assert gmail["baseline_unconfirmed_rate"] == 10.0
    assert gmail["baseline_p95_seconds"] == 100
    assert gmail["suggested_unconfirmed_percent"] == 15.0
    assert gmail["suggested_p95_seconds"] == 150
    assert providers["qq"]["usable_days"] == 1 and providers["qq"]["ready"] is False
    assert providers["microsoft"]["usable_days"] == 0
    assert providers["microsoft"]["suggested_unconfirmed_percent"] is None
    assert "result.initial_completed_at >= ? AND result.initial_completed_at < ?" in connection.sql
    assert "TO_CHAR(result.initial_completed_at" in connection.sql
    assert "result.initial_completed_at - job.created_at" in connection.sql
    assert "job.is_cache_refresh IS NOT TRUE" in connection.sql
    assert "GROUP BY day, provider" in connection.sql
    assert "PERCENTILE_CONT" in connection.sql
    assert connection.params == (
        "2026-08-08T00:00:00+00:00",
        "2026-08-15T00:00:00+00:00",
    )
    print("quality baseline smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
