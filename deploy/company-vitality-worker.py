"""Low-priority worker for Company Finder website vitality checks."""

from __future__ import annotations

import os
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait, FIRST_COMPLETED

from company_vitality import VitalityStore, probe_company


DATABASE_PATH = os.getenv(
    "COMPANY_FINDER_VITALITY_DATABASE_PATH",
    "/opt/verigo-company-finder/data/company_vitality.sqlite",
)
CONCURRENCY = max(1, min(4, int(os.getenv("COMPANY_FINDER_VITALITY_CONCURRENCY", "2"))))
PROBE_TIMEOUT = max(2.0, min(10.0, float(os.getenv("COMPANY_FINDER_VITALITY_TIMEOUT_SECONDS", "5"))))


def run() -> None:
    store = VitalityStore(DATABASE_PATH)
    released = store.release_claims()
    if released:
        print(f"released {released} orphaned vitality claims", flush=True)
    futures: dict[Future[dict[str, object]], dict[str, object]] = {}
    last_due_scan = 0.0
    with ThreadPoolExecutor(max_workers=CONCURRENCY, thread_name_prefix="company-vitality") as executor:
        while True:
            now = time.monotonic()
            if now - last_due_scan >= 60:
                store.enqueue_due()
                last_due_scan = now

            while len(futures) < CONCURRENCY:
                task = store.claim_next()
                if task is None:
                    break
                futures[executor.submit(probe_company, task, PROBE_TIMEOUT)] = task

            if not futures:
                time.sleep(1.0)
                continue

            completed, _ = wait(futures, timeout=1.0, return_when=FIRST_COMPLETED)
            for future in completed:
                task = futures.pop(future)
                try:
                    observation = future.result()
                except Exception:
                    observation = {"state": "uncertain", "reason": "worker_error"}
                store.complete(task, observation)


if __name__ == "__main__":
    run()
