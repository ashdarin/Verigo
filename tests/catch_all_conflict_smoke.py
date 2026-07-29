"""Regression checks for cross-worker Catch-all result reconciliation."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


temp_dir = Path(tempfile.mkdtemp(prefix="verigo-catch-all-conflict-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["VERIGO_DATABASE_PATH"] = str(temp_dir / "verigo.db")
os.environ["VERIGO_RESULTS_DIR"] = str(temp_dir / "results")

from app.core.catch_all import reconcile_catch_all_conflicts
from app.db.jobs import Job, job_store


def result(email: str, *, catch_all: bool, rejected: bool = False) -> dict[str, object]:
    return {
        "email": email,
        "progress_state": "completed",
        "domain_type": "catch-all" if catch_all else "normal",
        "deliverable": None if catch_all else not rejected,
        "valid": not rejected,
        "verification_method": "catch-all_detected" if catch_all else "standard",
        "smtp_result": "550 recipient rejected" if rejected else ("250 accepted" if not catch_all else "inconclusive"),
        "checks": {"smtp": True},
    }


items = [
    result("sales@example.test", catch_all=True),
    result("press@example.test", catch_all=False),
]
assert reconcile_catch_all_conflicts(items) == {"example.test"}
assert items[0]["domain_type"] == "inconclusive"
assert items[0]["deliverable"] is None
assert items[1]["deliverable"] is True

# A clear recipient rejection is also incompatible with a domain-wide
# Catch-all claim. This is the BMW regression: stale Catch-all and 550.
items = [
    result("sales@example.test", catch_all=True),
    result("missing@example.test", catch_all=False, rejected=True),
]
assert reconcile_catch_all_conflicts(items) == {"example.test"}
assert items[0]["domain_type"] == "inconclusive"
assert items[1]["deliverable"] is False

# Start the persistence scenario with the original confirmed-250 conflict.
items = [
    result("sales@example.test", catch_all=True),
    result("press@example.test", catch_all=False),
]
job = Job(id="catch-all-conflict", emails=["sales@example.test", "press@example.test"], worker_count=1)
job.results = [dict(items[0], original_index=0), dict(items[1], original_index=1)]
job_store.add(job)

# Simulate a late Catch-all callback arriving after a confirmed 250 callback.
job.results[0] = dict(result("sales@example.test", catch_all=True), original_index=0)
job_store.persist(job)
assert job_store.reconcile_catch_all_conflicts(job.id) == 2
stored = job_store.get(job.id)
assert stored is not None
assert stored.results[0]["domain_type"] == "inconclusive"
assert stored.results[0]["deliverable"] is None
assert stored.results[1]["deliverable"] is True
print("catch-all conflict smoke: ok")
