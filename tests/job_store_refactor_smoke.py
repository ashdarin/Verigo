from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import timedelta
from pathlib import Path


temp_dir = Path(tempfile.mkdtemp(prefix="verigo-store-refactor-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["VERIGO_DATABASE_PATH"] = str(temp_dir / "verigo.db")
os.environ["VERIGO_RESULTS_DIR"] = str(temp_dir / "results")

from app.db.jobs import Job, JobStore, utc_now
from app.db.sqlite import begin_immediate


store = JobStore()
store.initialize()


class LockThenAcquire:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, statement: str) -> None:
        assert statement == "BEGIN IMMEDIATE"
        self.calls += 1
        if self.calls < 3:
            raise sqlite3.OperationalError("database is locked")


lock_then_acquire = LockThenAcquire()
begin_immediate(lock_then_acquire)  # type: ignore[arg-type]
assert lock_then_acquire.calls == 3

assert store.service_mode() == "active"
store.set_service_mode("draining")
assert store.service_mode() == "draining"
store.set_service_mode("active")

parent = Job(
    id="parent", emails=["one@example.com", "two@example.com"], worker_count=1,
    status="running", execution_target="aggregate",
)
child = Job(
    id="child", emails=["two@example.com", "one@example.com"], worker_count=1,
    parent_id=parent.id, execution_target="gmail",
)
store.add(parent)
store.add(child)
store.link_child_results(child.id, parent.id, [1, 0])
store.upsert_results(child.id, [{
    "email": "two@example.com", "original_index": 0,
    "deliverable": True, "progress_state": "completed",
}])
assert store.get(parent.id).results[1]["deliverable"] is True

page_job = Job(
    id="page", emails=["valid@example.com", "invalid@example.com", "unknown@example.com"],
    worker_count=1, status="running",
)
store.add(page_job)
store.upsert_results(page_job.id, [
    {"email": "valid@example.com", "original_index": 0, "valid": True,
     "deliverable": True, "progress_state": "completed"},
    {"email": "invalid@example.com", "original_index": 1, "valid": True,
     "deliverable": False, "progress_state": "completed"},
    {"email": "unknown@example.com", "original_index": 2, "valid": True,
     "deliverable": None, "progress_state": "completed", "retry_updated": True},
])
available, results = store.result_page(page_job.id, offset=0, limit=1, deliverability="undeliverable")
assert available == 1 and [result["email"] for result in results] == ["invalid@example.com"]
available, results = store.result_page(page_job.id, offset=1, limit=1, search="example")
assert available == 3 and [result["email"] for result in results] == ["invalid@example.com"]
overview = store.result_overview(page_job.id)
assert (overview.total, overview.settled, overview.deliverable, overview.undeliverable, overview.unknown) == (3, 3, 1, 1, 1)
assert overview.review_updated is True

connection = store._connect()
connection.execute("UPDATE job_results SET query_fields_ready=0 WHERE job_id=?", (page_job.id,))
connection.close()
assert store.migrate_legacy_results()["result_query_fields"] >= 3
assert store.result_overview(page_job.id).deliverable == 1

# A terminal row may receive metadata updates, but it cannot be moved back to
# a waiting state by an old callback.
store.upsert_results(child.id, [{
    "email": "two@example.com", "original_index": 0, "progress_state": "pending",
}])
assert store.get(child.id).results[0]["progress_state"] == "completed"

remote = Job(
    id="remote", emails=["one@a.test", "two@b.test"], worker_count=1,
    execution_target="refactor-test",
)
store.add(remote)
first = store.claim_remote_lease("worker-a", "refactor-test", shard_size=2)
assert first is not None and first.lease_id and len(first.pending_indices) == 2
assert store.lease_accepts_results(remote.id, "worker-a", first.lease_id, first.pending_indices)
assert not store.lease_accepts_results(remote.id, "worker-a", first.lease_id, [99])

# Simulate a dead worker. The next claim expires the old lease and grants a
# fresh lease for the exact unfinished indexes.
connection = store._connect()
connection.execute(
    "UPDATE job_leases SET heartbeat_at=? WHERE id=?",
    ((utc_now() - timedelta(days=1)).isoformat(), first.lease_id),
)
connection.close()
second = store.claim_remote_lease("worker-b", "refactor-test", shard_size=2)
assert second is not None and second.lease_id != first.lease_id
assert not store.lease_valid(remote.id, "worker-a", first.lease_id)
assert second.pending_indices == [0, 1]
assert store.abandon_lease(remote.id, "worker-b", second.lease_id)

third = store.claim_remote_lease("worker-c", "refactor-test", shard_size=1)
assert third is not None and third.lease_id
assert store.abandon_lease(remote.id, "worker-c", third.lease_id)
assert store.get(remote.id).results[0]["progress_state"] == "pending"

# Provider/domain capacity is global. Adding a node must not multiply the
# configured Gmail or Microsoft concurrency budget.
assert store._scheduler_mx_key("person@gmail.com") == "gmail"
assert store._scheduler_mx_key("person@googlemail.com") == "gmail"
assert store._scheduler_mx_key("person@outlook.com") == "microsoft"
assert store._scheduler_mx_key("person@example.com") == "domain:example.com"

store.record_worker_seen("health-test", "fresh-node", capacity=3)
connection = store._connect()
connection.execute(
    "UPDATE worker_nodes SET last_seen_at=? WHERE target=? AND worker_id=?",
    ((utc_now() - timedelta(seconds=240)).isoformat(), "health-test", "fresh-node"),
)
connection.close()
state = store.reconcile_worker_nodes()
assert state["stale"] == 1
connection = store._connect()
assert connection.execute(
    "SELECT health FROM worker_nodes WHERE target=? AND worker_id=?", ("health-test", "fresh-node")
).fetchone()[0] == "stale"
connection.execute(
    "UPDATE worker_nodes SET last_seen_at=? WHERE target=? AND worker_id=?",
    ((utc_now() - timedelta(seconds=600)).isoformat(), "health-test", "fresh-node"),
)
connection.close()
state = store.reconcile_worker_nodes()
assert state["offline"] == 1

print("job store refactor smoke: ok")
