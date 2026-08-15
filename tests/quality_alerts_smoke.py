"""Unit checks for low-noise verification-quality alert thresholds."""
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.quality_alerts import evaluate  # noqa: E402


def main() -> int:
    low_volume = evaluate(
        {"review_backlog": 0, "providers": [{
            "provider": "qq", "processed": 12, "unconfirmed_rate": 90, "p95_seconds": 900,
        }]},
        minimum_sample=200, unconfirmed_percent=15, p95_seconds=180, review_backlog=25,
    )
    assert low_volume == []

    alerts = evaluate(
        {"review_backlog": 25, "providers": [
            {"provider": "gmail", "processed": 300, "unconfirmed_rate": 16.2, "latency_sample": 220, "p95_seconds": 181},
            {"provider": "microsoft", "processed": 240, "unconfirmed_rate": 4.0, "p95_seconds": 33},
        ]},
        minimum_sample=200, unconfirmed_percent=15, p95_seconds=180, review_backlog=25,
    )
    assert alerts == [
        "gmail unconfirmed=16.2%/300",
        "gmail p95=181s/220",
        "review backlog=25",
    ]
    sparse_latency = evaluate(
        {"review_backlog": 0, "providers": [{
            "provider": "qq", "processed": 300, "unconfirmed_rate": 2,
            "latency_sample": 12, "p95_seconds": 900,
        }]},
        minimum_sample=200, unconfirmed_percent=15, p95_seconds=180, review_backlog=25,
    )
    assert sparse_latency == []
    print("quality alerts smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
