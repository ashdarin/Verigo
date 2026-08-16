"""Confidence cache, partial-hit, and duplicate-probe regression checks."""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
temp_dir = Path(tempfile.mkdtemp(prefix="verigo-cache-"))
os.environ["VERIGO_DATABASE_URL"] = ""
os.environ["VERIGO_DATABASE_PATH"] = str(temp_dir / "verigo.db")
os.environ["VERIGO_RESULTS_DIR"] = str(temp_dir / "results")

from app.core.verification_cache_policy import cache_decision  # noqa: E402
from app.db.jobs import Job, JobStore, utc_now  # noqa: E402
from app.db.sqlite import connect as connect_sqlite  # noqa: E402
from app.tasks.verification import (  # noqa: E402
    VerificationTasks, cache_and_release_probe_results, finish_initial_job, waiting_result,
)
from app.api.schemas import WorkerResultsRequest  # noqa: E402
import app.tasks.verification as verification_module  # noqa: E402
import app.api.routes as routes_module  # noqa: E402


def result(email: str, **updates) -> dict:
    value = {
        "email": email,
        "deliverable": True,
        "valid": True,
        "smtp_code": "250",
        "progress_state": "completed",
    }
    value.update(updates)
    return value


def main() -> int:
    now = utc_now()
    first = cache_decision(result("first@example.test"), now=now)
    assert first and first.fresh_for == timedelta(days=7)
    repeated = cache_decision(
        result("repeat@example.test"), confirmation_count=2,
        first_confirmed_at=now - timedelta(days=2), now=now,
    )
    assert repeated and repeated.fresh_for == timedelta(days=14)
    stable = cache_decision(
        result("stable@example.test"), confirmation_count=2,
        first_confirmed_at=now - timedelta(days=8), now=now,
    )
    assert stable and stable.fresh_for == timedelta(days=30)
    assert cache_decision(result(
        "grey@example.test", deliverable=None, smtp_code="452",
        failure_reason="smtp_temporary",
    )) is None
    assert cache_decision(result(
        "catch@example.test", domain_type="catch-all",
    )) is None
    permanent = cache_decision(result(
        "gone@example.test", deliverable=False, smtp_code="550",
        failure_reason="smtp_permanent",
    ))
    assert permanent and permanent.fresh_for == timedelta(days=3)
    outlook = cache_decision(result(
        "gone@outlook.com", deliverable=False, smtp_code=None,
        verification_method="Outlook 账号验证", strategy="outlook_http",
    ))
    assert outlook and outlook.fresh_for == timedelta(days=3)
    full = cache_decision(result(
        "full@example.test", deliverable=False, smtp_code="452",
        delivery_block_reason="mailbox_full",
    ))
    assert full and full.fresh_for == timedelta(hours=2)

    store = JobStore()
    store._connect = lambda: connect_sqlite(temp_dir / "verigo.db")  # type: ignore[method-assign]
    store.initialize()
    store.cache_results([result("archive-catch@example.test", domain_type="catch-all")])
    assert store.cached_results(["archive-catch@example.test"]) == {}
    with store._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM verified_emails WHERE email=?",
            ("archive-catch@example.test",),
        ).fetchone()[0] == 0
    store.cache_results([result(
        "missing@outlook.com", deliverable=False, smtp_code=None,
        verification_method="Outlook 账号验证", strategy="outlook_http",
    )])
    assert store.cached_results(["missing@outlook.com"])["missing@outlook.com"]["deliverable"] is False

    owner = Job(
        id="probe-owner", emails=["shared@example.test"], worker_count=1,
        execution_target="cache-test", results=[waiting_result("shared@example.test", 0)],
    )
    waiter = Job(
        id="probe-waiter", emails=["shared@example.test"], worker_count=1,
        execution_target="cache-test", results=[waiting_result("shared@example.test", 0)],
    )
    store.add(owner)
    store.add(waiter)
    assert store.register_probe_candidates(owner.id, owner.results) == {"owned": 1, "waiting": 0}
    assert store.register_probe_candidates(waiter.id, waiter.results) == {"owned": 0, "waiting": 1}
    assert store.renew_probe_leases(owner.id) == 1
    affected = store.cache_results(
        [result("shared@example.test")], owner_job_id=owner.id,
    )
    assert affected == [waiter.id]
    shared = store.get(waiter.id)
    assert shared and shared.results[0]["deliverable"] is True
    assert shared.results[0]["cache_hit"] is True
    assert store.complete_probe_leases(owner.id, [result("shared@example.test")]) == []

    finalized_owner = Job(
        id="finalized-owner", emails=["finalized@example.test"], worker_count=1,
        execution_target="cache-test", results=[waiting_result("finalized@example.test", 0)],
    )
    finalized_waiter = Job(
        id="finalized-waiter", emails=["finalized@example.test"], worker_count=1,
        execution_target="cache-test", results=[waiting_result("finalized@example.test", 0)],
    )
    store.add(finalized_owner)
    store.add(finalized_waiter)
    store.register_probe_candidates(finalized_owner.id, finalized_owner.results)
    store.register_probe_candidates(finalized_waiter.id, finalized_waiter.results)
    original_store = verification_module.job_store
    original_publish = verification_module.publish_completed_result_objects
    verification_module.job_store = store
    verification_module.publish_completed_result_objects = lambda *_args, **_kwargs: None
    try:
        cache_and_release_probe_results(
            [result("finalized@example.test")], owner_job_id=finalized_owner.id,
        )
        finalized = store.get(finalized_waiter.id)
        assert finalized and finalized.status == "completed"
    finally:
        verification_module.job_store = original_store

    # Remote workers persist individual callbacks, then close the shard with an
    # empty result list. The completed shard must still populate the cache and
    # finalize duplicate waiters while another owner row remains pending.
    remote_owner = Job(
        id="remote-empty-complete-owner",
        emails=["remote-shared@example.test", "remote-pending@example.test"],
        worker_count=1,
        execution_target="remote-empty-complete-target",
        results=[
            waiting_result("remote-shared@example.test", 0),
            waiting_result("remote-pending@example.test", 1),
        ],
    )
    remote_waiter = Job(
        id="remote-empty-complete-waiter",
        emails=["remote-shared@example.test"], worker_count=1,
        execution_target="remote-empty-complete-target",
        results=[waiting_result("remote-shared@example.test", 0)],
    )
    store.add(remote_owner)
    store.add(remote_waiter)
    store.register_probe_candidates(remote_owner.id, remote_owner.results)
    store.register_probe_candidates(remote_waiter.id, remote_waiter.results)
    remote_lease = store.claim_remote_lease(
        "remote-cache-worker", "remote-empty-complete-target", shard_size=1,
    )
    assert remote_lease and remote_lease.id == remote_owner.id
    original_route_store = routes_module.job_store
    original_require_worker = routes_module.require_remote_worker
    original_receiver_protection = routes_module.apply_prospecting_receiver_protection
    original_verification_store = verification_module.job_store
    routes_module.job_store = store
    routes_module.require_remote_worker = (
        lambda *_args, **_kwargs: "remote-empty-complete-target"
    )
    routes_module.apply_prospecting_receiver_protection = lambda *_args, **_kwargs: None
    verification_module.job_store = store
    try:
        reported = routes_module.report_tencent_qq_results(
            "cache-test", remote_owner.id,
            WorkerResultsRequest(
                lease_id=remote_lease.lease_id,
                results=[result("remote-shared@example.test", original_index=0)],
            ),
            token="test-token", worker_id="remote-cache-worker",
        )
        assert reported["persisted"] == 1
        routes_module.complete_tencent_qq_job(
            "cache-test", remote_owner.id,
            WorkerResultsRequest(lease_id=remote_lease.lease_id, results=[]),
            token="test-token", worker_id="remote-cache-worker",
        )
        resumed = store.get(remote_waiter.id)
        assert resumed and resumed.status == "completed"
        assert resumed.results[0]["cache_hit"] is True
        cached_remote = store.cached_results(["remote-shared@example.test"])
        assert cached_remote["remote-shared@example.test"]["deliverable"] is True
    finally:
        routes_module.job_store = original_route_store
        routes_module.require_remote_worker = original_require_worker
        routes_module.apply_prospecting_receiver_protection = original_receiver_protection
        verification_module.job_store = original_verification_store

    # The public routing path must preserve cached rows across target partitions.
    original_route_store = routes_module.job_store
    original_route_tasks = routes_module.verification_tasks
    original_target = routes_module.email_execution_target
    original_verification_store = verification_module.job_store
    routes_module.job_store = store
    routes_module.verification_tasks = VerificationTasks()
    routes_module.email_execution_target = (
        lambda email, _owner_email, fast_local=False:
        "route-hit" if email.startswith("shared@") else "route-miss"
    )
    verification_module.job_store = store
    try:
        routed = routes_module.submit_routed_job(
            ["shared@example.test", "route-miss@example.test"], 2,
            owner_id="cache-owner", owner_email="owner@example.test",
        )
        assert routed.execution_target == "aggregate"
        assert routed.results[0]["cache_hit"] is True
        assert routed.results[1]["progress_state"] == "pending"
        routed_claim = store.claim_remote_lease(
            "route-worker", "route-miss", shard_size=10,
        )
        assert routed_claim and routed_claim.pending_indices == [0]
    finally:
        routes_module.job_store = original_route_store
        routes_module.verification_tasks = original_route_tasks
        routes_module.email_execution_target = original_target
        verification_module.job_store = original_verification_store
        verification_module.publish_completed_result_objects = original_publish

    transient_owner = Job(
        id="transient-owner", emails=["retry@example.test"], worker_count=1,
        execution_target="cache-test", results=[waiting_result("retry@example.test", 0)],
    )
    transient_waiter = Job(
        id="transient-waiter", emails=["retry@example.test"], worker_count=1,
        execution_target="cache-test", results=[waiting_result("retry@example.test", 0)],
    )
    store.add(transient_owner)
    store.add(transient_waiter)
    store.register_probe_candidates(transient_owner.id, transient_owner.results)
    store.register_probe_candidates(transient_waiter.id, transient_waiter.results)
    transient = result(
        "retry@example.test", deliverable=None, smtp_code="451",
        failure_reason="smtp_temporary",
    )
    assert store.cache_results([transient], owner_job_id=transient_owner.id) == []
    assert store.complete_probe_leases(transient_owner.id, [transient]) == [transient_waiter.id]
    assert "retry@example.test" not in store.cached_results(["retry@example.test"])

    expired_owner = Job(
        id="expired-owner", emails=["expired@example.test"], worker_count=1,
        execution_target="cache-test", results=[waiting_result("expired@example.test", 0)],
    )
    expired_waiter = Job(
        id="expired-waiter", emails=["expired@example.test"], worker_count=1,
        execution_target="cache-test", results=[waiting_result("expired@example.test", 0)],
    )
    store.add(expired_owner)
    store.add(expired_waiter)
    store.register_probe_candidates(expired_owner.id, expired_owner.results)
    store.register_probe_candidates(expired_waiter.id, expired_waiter.results)
    with store._connect() as connection:
        old = (utc_now() - timedelta(minutes=1)).isoformat()
        connection.execute("UPDATE verification_probe_leases SET expires_at=? WHERE email=?", (old, "expired@example.test"))
        connection.execute("UPDATE verification_probe_waiters SET expires_at=? WHERE email=?", (old, "expired@example.test"))
    assert store.release_expired_probe_waiters() == [expired_waiter.id]

    subset_owner = Job(
        id="subset-owner",
        emails=["one@subset.test", "two@subset.test"], worker_count=1,
        execution_target="cache-test",
        results=[waiting_result("one@subset.test", 0), waiting_result("two@subset.test", 1)],
    )
    subset_waiter = Job(
        id="subset-waiter",
        emails=["one@subset.test", "two@subset.test"], worker_count=1,
        execution_target="cache-test",
        results=[waiting_result("one@subset.test", 0), waiting_result("two@subset.test", 1)],
    )
    store.add(subset_owner)
    store.add(subset_waiter)
    store.register_probe_candidates(subset_owner.id, subset_owner.results)
    store.register_probe_candidates(subset_waiter.id, subset_waiter.results)
    assert store.release_probe_leases(subset_owner.id, ["one@subset.test"]) == [subset_waiter.id]
    with store._connect() as connection:
        remaining = connection.execute(
            "SELECT email FROM verification_probe_leases WHERE owner_job_id=?",
            (subset_owner.id,),
        ).fetchall()
    assert remaining == [("two@subset.test",)]

    cancel_owner = Job(
        id="cancel-owner", emails=["cancel@example.test"], worker_count=1,
        execution_target="cache-test", results=[waiting_result("cancel@example.test", 0)],
    )
    cancel_waiter = Job(
        id="cancel-waiter", emails=["cancel@example.test"], worker_count=1,
        execution_target="cache-test", results=[waiting_result("cancel@example.test", 0)],
    )
    store.add(cancel_owner)
    store.add(cancel_waiter)
    store.register_probe_candidates(cancel_owner.id, cancel_owner.results)
    store.register_probe_candidates(cancel_waiter.id, cancel_waiter.results)
    assert store.cancel_probe_jobs([cancel_owner.id]) == [cancel_waiter.id]
    with store._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM verification_probe_leases WHERE email=?",
            ("cancel@example.test",),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM verification_probe_waiters WHERE email=?",
            ("cancel@example.test",),
        ).fetchone()[0] == 0

    # A partial cache hit stays completed while the remote scheduler leases only the miss.
    cached = store.cached_results(["shared@example.test"])["shared@example.test"]
    original_store = verification_module.job_store
    verification_module.job_store = store
    try:
        mixed = VerificationTasks().submit(
            ["shared@example.test", "miss@example.test"], 2,
            execution_target="mixed-target", immediate_results=[cached],
        )
        assert mixed.status == "queued"
        assert mixed.results[0]["cache_hit"] is True
        assert mixed.results[1]["progress_state"] == "pending"
        claim = store.claim_remote_lease("mixed-worker", "mixed-target", shard_size=10)
        assert claim and claim.pending_indices == [1]
    finally:
        verification_module.job_store = original_store

    refresh_job = Job(
        id="refresh-completion", emails=["hot@example.test"], worker_count=1,
        execution_target="cache-test", is_cache_refresh=True,
        results=[result("hot@example.test")],
    )
    store.add(refresh_job)
    original_store = verification_module.job_store
    original_write_csv = verification_module.write_csv
    original_publish = verification_module.publish_completed_result_objects
    artifact_calls: list[str] = []
    verification_module.job_store = store
    verification_module.write_csv = lambda _job: artifact_calls.append("csv")
    verification_module.publish_completed_result_objects = lambda *_args, **_kwargs: artifact_calls.append("publish")
    try:
        completed_refresh = finish_initial_job(refresh_job)
        assert completed_refresh.status == "completed"
        assert artifact_calls == []
        assert store.cached_results(["hot@example.test"])["hot@example.test"]["deliverable"] is True
    finally:
        verification_module.job_store = original_store
        verification_module.write_csv = original_write_csv
        verification_module.publish_completed_result_objects = original_publish

    refresh = Job(
        id="refresh-priority", emails=["refresh@example.test"], worker_count=1,
        execution_target="priority-target", is_cache_refresh=True,
    )
    user = Job(
        id="user-priority", emails=["user@example.test"], worker_count=1,
        execution_target="priority-target",
    )
    store.add(refresh)
    store.add(user)
    chosen = store.claim_remote_lease("priority-worker", "priority-target", shard_size=1)
    assert chosen and chosen.id == user.id

    report = store.cache_report()
    assert report["totals"]["lookups"] >= 2
    assert report["current"]["deliverable"] >= 1
    print("verification cache smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
