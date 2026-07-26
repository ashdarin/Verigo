from __future__ import annotations

import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path


temp_dir = Path(tempfile.mkdtemp(prefix="verigo-store-refactor-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["VERIGO_DATABASE_PATH"] = str(temp_dir / "verigo.db")
os.environ["VERIGO_RESULTS_DIR"] = str(temp_dir / "results")

from app.db.jobs import Job, JobStore, utc_now


store = JobStore()
store.initialize()

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

print("job store refactor smoke: ok")
