"""Unit checks for queue-health alert formatting and threshold results."""
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.queue_health_alerts import QueueHealth, evaluate  # noqa: E402


def main() -> int:
    assert evaluate(QueueHealth()) == []
    alerts = evaluate(
        QueueHealth(
            oldest_queued_seconds=901,
            running_without_lease=2,
            oldest_running_without_lease_seconds=301,
            stale_workers=3,
            long_cooldowns=(("mx:mx.example.test", 1801),),
        )
    )
    assert alerts == [
        "oldest queued job=901s",
        "running without active lease=2/301s",
        "stale worker heartbeats=3",
        "provider cooldown exceeds normal window: mx:mx.example.test:1801s",
    ]
    print("queue health alerts smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
