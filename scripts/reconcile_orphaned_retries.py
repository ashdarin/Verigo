"""Settle visible verification reviews that no longer have an active child job."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.tasks.verification import reconcile_orphaned_background_retries  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--grace-seconds", type=int, default=15 * 60)
    parser.add_argument("--parent-limit", type=int, default=1000)
    args = parser.parse_args()
    summary = reconcile_orphaned_background_retries(
        grace_seconds=max(60, args.grace_seconds),
        parent_limit=max(1, min(args.parent_limit, 1000)),
    )
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
