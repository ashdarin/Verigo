"""Exercise the extracted verification worker without creating child processes."""

from __future__ import annotations

from pathlib import Path
from queue import Queue
import sys


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.core.verification_worker as verification_worker
from app.core.verification_worker import run_verification_worker


class FakeVerifier:
    def __init__(self):
        self.dns_cache = {}

    def is_consumer_fix_supported(self, domain):
        return False

    def get_consumer_fix_strategy(self, domain):
        return None

    def verify_email_comprehensive(self, email, process_id):
        return {
            "email": email,
            "process_id": process_id,
            "strategy": "fast",
        }


email_queue = Queue()
result_queue = Queue()
progress_queue = Queue()
email_queue.put(("person@example.com", 4))
email_queue.put(None)

prefetched: list[str] = []
enrichment_network_flags: list[bool] = []
verification_worker.prefetch_disposable_provider = prefetched.append
original_enrich = verification_worker.enrich_disposable_provider


def record_enrichment(result, *, allow_network=True):
    enrichment_network_flags.append(allow_network)
    return original_enrich(result, allow_network=allow_network)


verification_worker.enrich_disposable_provider = record_enrichment

run_verification_worker(
    3,
    email_queue,
    result_queue,
    progress_queue,
    {},
    None,
    FakeVerifier,
)

result = result_queue.get_nowait()
assert result["email"] == "person@example.com"
assert result["process_id"] == 3
assert result["original_index"] == 4
assert result["dns_cached"] is False
assert result["timings_ms"]["worker_total"] >= 0
assert prefetched == ["person@example.com"]
assert enrichment_network_flags == [False]

progress = []
while not progress_queue.empty():
    progress.append(progress_queue.get_nowait())

assert progress[0]["status"] == "processing"
assert progress[-1]["status"] == "completed"
assert progress[-1]["processed"] == 1

print("verification worker smoke: ok")
