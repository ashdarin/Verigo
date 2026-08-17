"""Ensure a completed shard can dispatch one bounded cross-route review."""
from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path


temp_dir = Path(tempfile.mkdtemp(prefix="verigo-early-review-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["VERIGO_DATABASE_URL"] = ""
os.environ["VERIGO_DATABASE_PATH"] = str(temp_dir / "verigo.db")
os.environ["VERIGO_RESULTS_DIR"] = str(temp_dir / "results")

from app.config import settings as base_settings  # noqa: E402
from app.core import smtp_cross_route  # noqa: E402
from app.api import routes  # noqa: E402
from app.api.schemas import WorkerResultsRequest  # noqa: E402
from app.db.jobs import Job, JobStore  # noqa: E402
from app.db.sqlite import connect as connect_sqlite  # noqa: E402
from app.tasks import verification  # noqa: E402


active_settings = replace(
    base_settings,
    smtp_cross_route_enabled=True,
    smtp_cross_route_shadow_mode=False,
    smtp_cross_route_target="local",
    smtp_cross_route_max_per_email=1,
    smtp_cross_route_concurrency=1,
    smtp_cross_route_per_mx_concurrency=1,
)
verification.settings = active_settings
smtp_cross_route.settings = active_settings


class FakeReviewEvents:
    def __init__(self) -> None:
        self.events = []

    def record_many(self, events) -> bool:
        self.events.extend(events)
        return True


def temporary(email: str, index: int) -> dict:
    return {
        "email": email,
        "original_index": index,
        "progress_state": "completed",
        "deliverable": None,
        "valid": True,
        "smtp_code": "452",
        "smtp_result": "452 4.3.1 temporary recipient failure",
        "smtp_raw_result": "452 4.3.1 temporary recipient failure",
        "mx_records": ["aspmx.l.google.com"],
    }


store = JobStore()
store._connect = lambda: connect_sqlite(temp_dir / "verigo.db")  # type: ignore[method-assign]
store.initialize()
parent = Job(
    id="early-review-parent",
    emails=["person@gmail.com", "slow@example.test"],
    worker_count=4,
    status="running",
    execution_target="aggregate",
)
child = Job(
    id="early-review-child",
    emails=["person@gmail.com"],
    worker_count=4,
    status="running",
    execution_target="gmail",
    parent_id=parent.id,
)
store.add(parent)
store.add(child)
store.link_child_results(child.id, parent.id, [0])
store.upsert_results(child.id, [temporary("person@gmail.com", 0)])

original_store = verification.job_store
original_events = verification.smtp_review_event_store
original_notify = verification._notify_retry_target
review_events = FakeReviewEvents()
verification.job_store = store
verification.smtp_review_event_store = review_events
verification._notify_retry_target = lambda _job: None
try:
    completed = store.results_for_job(child.id)
    initial_times = store.initial_completion_times(parent.id, ["person@gmail.com"])
    assert "person@gmail.com" in initial_times
    verification.enqueue_initial_smtp_reviews(child, completed)
    verification.enqueue_initial_smtp_reviews(child, completed)

    retries = store.retry_children(parent.id)
    assert len(retries) == 1
    retry = retries[0]
    assert retry.retry_route == "alternate_route"
    assert retry.execution_target == "local"
    assert retry.origin_execution_target == "gmail"
    assert retry.parent_id is None
    assert len(review_events.events) == 1
    assert review_events.events[0].initial_completed_at == initial_times["person@gmail.com"]

    review_child = replace(child, retry_parent_id=parent.id)
    verification.enqueue_initial_smtp_reviews(review_child, completed)
    assert len(store.retry_children(parent.id)) == 1
finally:
    verification.job_store = original_store
    verification.smtp_review_event_store = original_events
    verification._notify_retry_target = original_notify


# Remote callbacks must dispatch a completed shard before the root job drains.
api_store = JobStore()
api_store._connect = lambda: connect_sqlite(temp_dir / "remote.db")  # type: ignore[method-assign]
api_store.initialize()
remote = Job(
    id="early-review-remote",
    emails=["person@gmail.com", "slow@example.test"],
    worker_count=2,
    execution_target="gmail",
)
api_store.add(remote)
lease = api_store.claim_remote_lease("early-review-worker", "gmail", shard_size=1)
assert lease and lease.pending_indices == [0]
original_route_store = routes.job_store
original_require_worker = routes.require_remote_worker
original_require_remote_job = routes.require_remote_job
original_receiver_protection = routes.apply_prospecting_receiver_protection
original_dispatch = routes.enqueue_initial_smtp_reviews
original_cache_release = routes.cache_and_release_probe_results
original_sync_parent = routes.sync_parent_job
dispatched: list[tuple[Job, list[dict]]] = []
routes.job_store = api_store
routes.require_remote_worker = lambda *_args, **_kwargs: "gmail"
routes.require_remote_job = lambda job_id, *_args, **_kwargs: api_store.get(job_id, include_results=False)
routes.apply_prospecting_receiver_protection = lambda *_args, **_kwargs: None
routes.enqueue_initial_smtp_reviews = lambda source, rows: dispatched.append((source, rows))
routes.cache_and_release_probe_results = lambda *_args, **_kwargs: None
routes.sync_parent_job = lambda _job: None
try:
    routes.report_tencent_qq_results(
        "gmail",
        remote.id,
        WorkerResultsRequest(
            lease_id=lease.lease_id,
            results=[temporary("person@gmail.com", 0)],
        ),
        token="test-token",
        worker_id="early-review-worker",
    )
    assert len(dispatched) == 1
    assert dispatched[0][0].id == remote.id
    assert [row["email"] for row in dispatched[0][1]] == ["person@gmail.com"]
finally:
    routes.job_store = original_route_store
    routes.require_remote_worker = original_require_worker
    routes.require_remote_job = original_require_remote_job
    routes.apply_prospecting_receiver_protection = original_receiver_protection
    routes.enqueue_initial_smtp_reviews = original_dispatch
    routes.cache_and_release_probe_results = original_cache_release
    routes.sync_parent_job = original_sync_parent

print("early smtp review dispatch smoke: ok")
