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

from app.config import settings
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
assert store.stop(child.id) is not None

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

fallback = Job(
    id="remote-fallback", emails=["one@fallback.test"], worker_count=1,
    execution_target="gmail",
)
store.add(fallback)
connection = store._connect()
connection.execute(
    "UPDATE jobs SET created_at=? WHERE id=?",
    ((utc_now() - timedelta(minutes=10)).isoformat(), fallback.id),
)
connection.close()
assert store.reroute_stale_queued_jobs("gmail", "local", 60, "Remote worker unavailable") == 1
assert store.get(fallback.id).execution_target == "local"
assert store.stop(fallback.id) is not None

# A remote node first serves its own target. When that queue is empty, it
# joins the local pool immediately instead of idling.
shared_pool = Job(
    id="shared-pool", emails=["one@shared-pool.test"], worker_count=1,
    execution_target="local",
)
store.add(shared_pool)
connection = store._connect()
connection.execute("UPDATE jobs SET created_at=? WHERE id=?", ("1970-01-01T00:00:00+00:00", shared_pool.id))
connection.close()
shared_lease = store.claim_remote_lease(
    "gmail-worker", "gmail", shard_size=1, allow_local_fallback=True,
)
assert shared_lease is not None and shared_lease.id == shared_pool.id
assert shared_lease.execution_target == "gmail"
assert store.get(shared_pool.id).execution_target == "gmail"
assert store.abandon_lease(shared_pool.id, "gmail-worker", shared_lease.lease_id or "")

# Provider/domain capacity is global. Adding a node must not multiply the
# configured Gmail or Microsoft concurrency budget.
assert store._scheduler_mx_key("person@gmail.com") == "gmail"
assert store._scheduler_mx_key("person@googlemail.com") == "gmail"
assert store._scheduler_mx_key("person@outlook.com") == "microsoft"
assert store._scheduler_mx_key("person@example.com") == "domain:example.com"
assert store._scheduler_key_for_mx_host("aspmx.l.google.com.") == "gmail"
assert store._scheduler_key_for_mx_host("tenant.mail.protection.outlook.com") == "microsoft"

# A provider at capacity must not block an unrelated runnable domain behind it.
saturated = Job(
    id="saturated-gmail",
    emails=[f"gmail-{index}@gmail.com" for index in range(26)],
    worker_count=1,
    execution_target="scheduler-test",
)
runnable = Job(
    id="runnable-domain",
    emails=["ready@independent.test"],
    worker_count=1,
    execution_target="scheduler-test",
)
store.add(saturated)
store.add(runnable)
gmail_lease = store.claim_remote_lease("scheduler-a", "scheduler-test", shard_size=25)
assert gmail_lease is not None and gmail_lease.id == saturated.id
assert len(gmail_lease.pending_indices) == 25
domain_lease = store.claim_remote_lease("scheduler-b", "scheduler-test", shard_size=25)
assert domain_lease is not None and domain_lease.id == runnable.id
assert domain_lease.pending_indices == [0]
assert store.abandon_lease(saturated.id, "scheduler-a", gmail_lease.lease_id or "")
assert store.abandon_lease(runnable.id, "scheduler-b", domain_lease.lease_id or "")

# Small shards allow an idle node to steal unfinished work from a peer's job.
stealable = Job(
    id="stealable",
    emails=["one@steal-a.test", "two@steal-b.test"],
    worker_count=1,
    execution_target="steal-test",
)
store.add(stealable)
steal_first = store.claim_remote_lease("steal-a", "steal-test", shard_size=1)
steal_second = store.claim_remote_lease("steal-b", "steal-test", shard_size=1)
assert steal_first is not None and steal_first.pending_indices == [0]
assert steal_second is not None and steal_second.id == stealable.id
assert steal_second.pending_indices == [1]
assert store.abandon_lease(stealable.id, "steal-a", steal_first.lease_id or "")
assert store.abandon_lease(stealable.id, "steal-b", steal_second.lease_id or "")

# The next successful lease rotates to another owner when both have runnable work.
fair_first = Job(
    id="fair-first", emails=["one@fair-first.test"], worker_count=1,
    owner_id="owner-first", execution_target="fair-test",
)
fair_second = Job(
    id="fair-second", emails=["one@fair-second.test"], worker_count=1,
    owner_id="owner-second", execution_target="fair-test",
)
store.add(fair_first)
store.add(fair_second)
fair_lease_one = store.claim_remote_lease("fair-a", "fair-test", shard_size=1)
fair_lease_two = store.claim_remote_lease("fair-b", "fair-test", shard_size=1)
assert fair_lease_one is not None and fair_lease_one.id == fair_first.id
assert fair_lease_two is not None and fair_lease_two.id == fair_second.id
assert store.abandon_lease(fair_first.id, "fair-a", fair_lease_one.lease_id or "")
assert store.abandon_lease(fair_second.id, "fair-b", fair_lease_two.lease_id or "")

# Receiver pressure halves a domain's limit once per cooldown. Stable outcomes
# raise it one slot at a time after the configured success threshold.
pressure = Job(
    id="adaptive-pressure",
    emails=["first@adaptive.test"],
    worker_count=1,
    execution_target="profile-test",
)
store.add(pressure)
pressure_lease = store.claim_remote_lease("profile-a", "profile-test", shard_size=1)
assert pressure_lease is not None
store.upsert_results(pressure.id, [{
    "email": "first@adaptive.test", "original_index": 0,
    "progress_state": "completed", "smtp_result": "421 rate limited",
}])
assert store.complete_lease(pressure.id, "profile-a", pressure_lease.lease_id or "")
connection = store._connect()
profile = connection.execute(
    "SELECT current_limit, success_streak, pressure_events FROM scheduler_domain_profiles "
    "WHERE scheduler_key='domain:adaptive.test'"
).fetchone()
assert profile == (2, 0, 1)
connection.execute(
    "UPDATE scheduler_domain_profiles SET cooldown_until=? WHERE scheduler_key='domain:adaptive.test'",
    ((utc_now() - timedelta(seconds=1)).isoformat(),),
)
connection.close()

previous_success_step = settings.scheduler_successes_per_step
object.__setattr__(settings, "scheduler_successes_per_step", 2)
success = Job(
    id="adaptive-success",
    emails=["second@adaptive.test", "third@adaptive.test"],
    worker_count=1,
    execution_target="profile-test",
)
store.add(success)
success_lease = store.claim_remote_lease("profile-b", "profile-test", shard_size=2)
assert success_lease is not None and len(success_lease.pending_indices) == 2
store.upsert_results(success.id, [
    {
        "email": email,
        "original_index": index,
        "progress_state": "completed",
        "checks": {"smtp": True},
        "smtp_result": "250 accepted",
    }
    for index, email in enumerate(success.emails)
])
assert store.complete_lease(success.id, "profile-b", success_lease.lease_id or "")
object.__setattr__(settings, "scheduler_successes_per_step", previous_success_step)
connection = store._connect()
assert connection.execute(
    "SELECT current_limit, successes FROM scheduler_domain_profiles "
    "WHERE scheduler_key='domain:adaptive.test'"
).fetchone() == (3, 2)
connection.close()

# Prospecting tasks can fill a larger first batch and ramp faster after stable
# SMTP outcomes. Receiver pressure still cancels the discovery-specific floor.
connection = store._connect()
connection.execute("CREATE TABLE prospecting_runs (verification_job_id TEXT PRIMARY KEY)")
connection.close()
previous_prospecting_initial = settings.prospecting_scheduler_initial_domain_concurrency
previous_prospecting_step = settings.prospecting_scheduler_successes_per_step
previous_prospecting_size = settings.prospecting_scheduler_step_size
object.__setattr__(settings, "prospecting_scheduler_initial_domain_concurrency", 8)
object.__setattr__(settings, "prospecting_scheduler_successes_per_step", 8)
object.__setattr__(settings, "prospecting_scheduler_step_size", 2)
prospecting_fast = Job(
    id="prospecting-fast",
    emails=[f"person-{index}@prospecting-fast.test" for index in range(16)],
    worker_count=8,
    execution_target="profile-test",
)
store.add(prospecting_fast)
connection = store._connect()
connection.execute(
    "INSERT INTO prospecting_runs(verification_job_id) VALUES (?)", (prospecting_fast.id,)
)
connection.close()
prospecting_lease = store.claim_remote_lease("prospecting-a", "profile-test", shard_size=25)
assert prospecting_lease is not None and len(prospecting_lease.pending_indices) == 8
store.upsert_results(prospecting_fast.id, [{
    "email": prospecting_fast.emails[index], "original_index": index,
    "progress_state": "completed", "checks": {"smtp": True}, "smtp_result": "250 accepted",
} for index in prospecting_lease.pending_indices])
assert store.complete_lease(prospecting_fast.id, "prospecting-a", prospecting_lease.lease_id or "")
connection = store._connect()
assert connection.execute(
    "SELECT current_limit FROM scheduler_domain_profiles "
    "WHERE scheduler_key='domain:prospecting-fast.test'"
).fetchone() == (6,)
connection.close()

prospecting_pressure = Job(
    id="prospecting-pressure",
    emails=[f"person-{index}@prospecting-pressure.test" for index in range(8)],
    worker_count=8,
    execution_target="profile-test",
)
store.add(prospecting_pressure)
connection = store._connect()
connection.execute(
    "INSERT INTO prospecting_runs(verification_job_id) VALUES (?)", (prospecting_pressure.id,)
)
connection.close()
pressure_lease = store.claim_remote_lease("prospecting-b", "profile-test", shard_size=25)
assert pressure_lease is not None and len(pressure_lease.pending_indices) == 8
store.upsert_results(prospecting_pressure.id, [{
    "email": prospecting_pressure.emails[index], "original_index": index,
    "progress_state": "completed", "smtp_result": "421 rate limited",
} for index in pressure_lease.pending_indices])
assert store.complete_lease(prospecting_pressure.id, "prospecting-b", pressure_lease.lease_id or "")
prospecting_after_pressure = Job(
    id="prospecting-after-pressure",
    emails=[f"retry-{index}@prospecting-pressure.test" for index in range(8)],
    worker_count=8,
    execution_target="profile-test",
)
store.add(prospecting_after_pressure)
connection = store._connect()
connection.execute(
    "INSERT INTO prospecting_runs(verification_job_id) VALUES (?)", (prospecting_after_pressure.id,)
)
connection.execute(
    "UPDATE scheduler_domain_profiles SET cooldown_until=? "
    "WHERE scheduler_key='domain:prospecting-pressure.test'",
    ((utc_now() - timedelta(seconds=1)).isoformat(),),
)
connection.close()
after_pressure_lease = store.claim_remote_lease("prospecting-c", "profile-test", shard_size=25)
assert after_pressure_lease is not None and len(after_pressure_lease.pending_indices) == 2
assert store.abandon_lease(
    prospecting_after_pressure.id, "prospecting-c", after_pressure_lease.lease_id or ""
)
object.__setattr__(settings, "prospecting_scheduler_initial_domain_concurrency", previous_prospecting_initial)
object.__setattr__(settings, "prospecting_scheduler_successes_per_step", previous_prospecting_step)
object.__setattr__(settings, "prospecting_scheduler_step_size", previous_prospecting_size)

# A completed verification teaches later scheduling to use the actual shared MX.
routed = Job(
    id="routed-domain", emails=["first@tenant-one.test"], worker_count=1,
    execution_target="route-test",
)
store.add(routed)
routed_lease = store.claim_remote_lease("route-a", "route-test", shard_size=1)
assert routed_lease is not None
store.upsert_results(routed.id, [{
    "email": "first@tenant-one.test", "original_index": 0,
    "progress_state": "completed", "checks": {"smtp": True},
    "smtp_result": "250 accepted", "mx_records": ["mx.shared-host.test."],
}])
assert store.complete_lease(routed.id, "route-a", routed_lease.lease_id or "")
connection = store._connect()
assert connection.execute(
    "SELECT scheduler_key FROM scheduler_domain_routes WHERE domain='tenant-one.test'"
).fetchone() == ("mx:mx.shared-host.test",)
connection.close()
follow_up = Job(
    id="routed-follow-up", emails=["second@tenant-one.test"], worker_count=1,
    execution_target="route-test",
)
store.add(follow_up)
follow_up_lease = store.claim_remote_lease("route-b", "route-test", shard_size=1)
assert follow_up_lease is not None
connection = store._connect()
assert connection.execute(
    "SELECT mx_key FROM mx_scheduler_leases WHERE lease_id=?", (follow_up_lease.lease_id,)
).fetchone() == ("mx:mx.shared-host.test",)
connection.close()
assert store.abandon_lease(follow_up.id, "route-b", follow_up_lease.lease_id or "")

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

# Queue admission counts user-visible tasks, not their internal child shards or
# background retry jobs. A configured limit is therefore predictable to users.
original_database_path = settings.database_path
object.__setattr__(settings, "database_path", temp_dir / "quota.db")
quota_store = JobStore()
quota_parent = Job(id="quota-parent", emails=["parent@quota.test"], worker_count=1)
quota_child = Job(
    id="quota-child", emails=["child@quota.test"], worker_count=1,
    parent_id=quota_parent.id,
)
quota_store.add(quota_parent, max_active=1)
quota_store.add(quota_child)
try:
    quota_store.add(Job(id="quota-next", emails=["next@quota.test"], worker_count=1), max_active=1)
except RuntimeError:
    pass
else:
    raise AssertionError("queue limit must reject a second visible task")
object.__setattr__(settings, "database_path", original_database_path)

print("job store refactor smoke: ok")
