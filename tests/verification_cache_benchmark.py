"""Repeatable local guardrail for cache lookup and write overhead."""
from __future__ import annotations

import os
import statistics
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
temp_dir = Path(tempfile.mkdtemp(prefix="verigo-cache-bench-"))
os.environ["VERIGO_DATABASE_URL"] = ""
os.environ["VERIGO_DATABASE_PATH"] = str(temp_dir / "verigo.db")

from app.db.jobs import JobStore  # noqa: E402
from app.db.sqlite import connect as connect_sqlite  # noqa: E402


def elapsed_ms(callable_) -> float:
    started = time.perf_counter()
    callable_()
    return (time.perf_counter() - started) * 1000


def percentile(samples: list[float], value: float) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * value))]


def main() -> int:
    store = JobStore()
    store._connect = lambda: connect_sqlite(temp_dir / "verigo.db")  # type: ignore[method-assign]
    store.initialize()
    emails = [f"bench-{index}@example.test" for index in range(5000)]
    results = [
        {
            "email": email, "deliverable": True, "valid": True,
            "smtp_code": "250", "progress_state": "completed",
        }
        for email in emails
    ]
    write_ms = elapsed_ms(lambda: store.cache_results(results))
    hit_samples = [elapsed_ms(lambda: store.cached_results(emails)) for _ in range(20)]
    misses = [f"missing-{index}@example.test" for index in range(5000)]
    miss_samples = [elapsed_ms(lambda: store.cached_results(misses)) for _ in range(20)]
    report_ms = elapsed_ms(store.cache_report)
    hit_p95 = percentile(hit_samples, 0.95)
    miss_p95 = percentile(miss_samples, 0.95)
    assert hit_p95 < 500
    assert miss_p95 < 250
    print(
        "verification cache benchmark: "
        f"write_5000_ms={write_ms:.1f} "
        f"hit_5000_p50_ms={statistics.median(hit_samples):.1f} "
        f"hit_5000_p95_ms={hit_p95:.1f} "
        f"miss_5000_p50_ms={statistics.median(miss_samples):.1f} "
        f"miss_5000_p95_ms={miss_p95:.1f} report_ms={report_ms:.1f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
