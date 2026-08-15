"""Evaluate bounded verification-quality metrics against operational thresholds."""
from __future__ import annotations

import json
import os
from typing import Any


def _number(value: object, default: float = 0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def evaluate(
    payload: dict[str, Any], *, minimum_sample: int, unconfirmed_percent: float,
    p95_seconds: float, review_backlog: int,
) -> list[str]:
    alerts: list[str] = []
    for row in payload.get("providers") or []:
        if not isinstance(row, dict):
            continue
        provider = str(row.get("provider") or "other")
        processed = int(_number(row.get("processed")))
        if processed < minimum_sample:
            continue
        unconfirmed = _number(row.get("unconfirmed_rate"), -1)
        if unconfirmed >= unconfirmed_percent:
            alerts.append(f"{provider} unconfirmed={unconfirmed:.1f}%/{processed}")
        p95 = _number(row.get("p95_seconds"), -1)
        if p95 >= p95_seconds:
            alerts.append(f"{provider} p95={p95:.0f}s/{processed}")
    backlog = int(_number(payload.get("review_backlog")))
    if backlog >= review_backlog:
        alerts.append(f"review backlog={backlog}")
    return alerts


def main() -> int:
    payload = json.loads(os.environ["VERIGO_QUALITY_PAYLOAD"])
    alerts = evaluate(
        payload,
        minimum_sample=max(1, int(os.environ.get("VERIGO_MONITOR_QUALITY_MIN_SAMPLE", "200"))),
        unconfirmed_percent=max(0, _number(os.environ.get("VERIGO_MONITOR_UNCONFIRMED_PERCENT", "15"))),
        p95_seconds=max(1, _number(os.environ.get("VERIGO_MONITOR_P95_SECONDS", "180"))),
        review_backlog=max(1, int(os.environ.get("VERIGO_MONITOR_REVIEW_BACKLOG", "25"))),
    )
    print("; ".join(alerts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
