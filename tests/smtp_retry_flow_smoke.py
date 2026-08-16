from __future__ import annotations

import inspect
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.jobs import Job, JobStore
from app.tasks import verification


class FakeStore:
    def __init__(self, parent: Job) -> None:
        self.parent = parent
        self.added: list[Job] = []
        self.upserted: list[dict] = []

    def get(self, job_id: str) -> Job | None:
        return self.parent if job_id == self.parent.id else None

    def persist(self, job: Job) -> None:
        assert job.id == self.parent.id

    def cache_results(self, results: list[dict], *, owner_job_id: str | None = None) -> list[str]:
        self.cached = results
        return []

    def complete_probe_leases(self, owner_job_id: str, results: list[dict]) -> list[str]:
        return []

    def upsert_results(self, job_id: str, results: list[dict]) -> None:
        assert job_id == self.parent.id
        self.upserted.extend(results)

    def record_catch_all(self, job: Job) -> None:
        assert job.id == self.parent.id

    def retry_children(self, parent_id: str) -> list[Job]:
        assert parent_id == self.parent.id
        return self.added

    def add(self, job: Job) -> None:
        self.added.append(job)

    def orphaned_retry_parent_ids(self, _cutoff, _limit: int) -> list[str]:
        return [self.parent.id]

    def has_active_retry_child(self, parent_id: str) -> bool:
        assert parent_id == self.parent.id
        return False


verification.write_csv = lambda _job: None
verification.publish_completed_result_objects = lambda _job, _results=None: None
verification._notify_retry_target = lambda _job: None

temporary_parent = Job(
    id="retry-policy-temporary-parent",
    emails=["person@example.test"],
    worker_count=1,
    results=[{
        "email": "person@example.test",
        "smtp_result": "452 4.3.1 temporary recipient failure",
        "retry_state": "scheduled",
    }],
)
temporary_store = FakeStore(temporary_parent)
verification.job_store = temporary_store
temporary_child = Job(
    id="retry-policy-temporary-child",
    emails=temporary_parent.emails,
    worker_count=1,
    retry_parent_id=temporary_parent.id,
    temporary_retry_attempts=1,
    results=[{"email": "person@example.test", "smtp_result": "452 4.3.1 temporary recipient failure"}],
)
verification.finish_background_retry(temporary_child)
temporary_result = temporary_parent.results[0]
assert temporary_store.added == []
assert len(temporary_store.upserted) == 1
assert temporary_result["deliverable"] is None
assert temporary_result["retry_policy"] == "never"
assert temporary_result["retry_state"] == "completed"
assert "自动复核已结束" in temporary_result["smtp_result"]

greylist_parent = Job(
    id="retry-policy-greylist-parent",
    emails=["person@example.test"],
    worker_count=1,
    results=[{
        "email": "person@example.test",
        "smtp_result": "450 4.7.1 greylisted",
        "retry_state": "scheduled",
    }],
)
greylist_store = FakeStore(greylist_parent)
verification.job_store = greylist_store
greylist_child = Job(
    id="retry-policy-greylist-child",
    emails=greylist_parent.emails,
    worker_count=1,
    retry_parent_id=greylist_parent.id,
    temporary_retry_attempts=1,
    results=[{"email": "person@example.test", "smtp_result": "450 4.7.1 greylisted"}],
)
verification.finish_background_retry(greylist_child)
assert len(greylist_store.added) == 1
assert greylist_store.added[0].temporary_retry_attempts == 2
assert greylist_parent.results[0]["retry_state"] == "scheduled"
assert greylist_parent.results[0]["retry_max_attempts"] == 2

orphan_parent = Job(
    id="retry-policy-orphan-parent",
    emails=["good@example.test", "temporary@example.test", "missing@example.test"],
    worker_count=1,
    results=[
        {"email": "good@example.test", "smtp_result": "451 temporary", "retry_at": "2026-01-01T00:00:00+00:00", "retry_state": "scheduled"},
        {"email": "temporary@example.test", "smtp_result": "452 temporary", "retry_at": "2026-01-01T00:00:00+00:00", "retry_state": "scheduled"},
        {"email": "missing@example.test", "smtp_result": "421 temporary", "retry_at": "2026-01-01T00:00:00+00:00", "retry_state": "scheduled"},
    ],
)
orphan_store = FakeStore(orphan_parent)
orphan_store.added.append(Job(
    id="retry-policy-orphan-child",
    emails=orphan_parent.emails[:2],
    worker_count=1,
    status="completed",
    retry_parent_id=orphan_parent.id,
    temporary_retry_attempts=1,
    results=[
        {"email": "good@example.test", "deliverable": True, "smtp_result": "250 accepted"},
        {"email": "temporary@example.test", "deliverable": None, "smtp_result": "452 temporary"},
    ],
))
verification.job_store = orphan_store
summary = verification.reconcile_orphaned_background_retries(grace_seconds=60, parent_limit=10)
assert summary == {"parents": 1, "results": 3, "recovered": 2, "failed": 1}
assert len(orphan_store.upserted) == 3
assert orphan_parent.results[0]["deliverable"] is True
assert orphan_parent.results[0]["retry_state"] == "completed"
assert orphan_parent.results[1]["retry_state"] == "completed"
assert orphan_parent.results[2]["retry_state"] == "failed"
assert all("retry_at" not in result for result in orphan_parent.results)
for function in (
    verification.enqueue_background_retry,
    verification.finish_background_retry,
    verification.finish_background_retry_failure,
):
    source = inspect.getsource(function)
    assert "job_store.upsert_results" in source
    assert "job_store.persist(parent)" not in source
upsert_source = inspect.getsource(JobStore._upsert_results)
assert upsert_source.count("job_results.result_json <> excluded.result_json") == 2

print("smtp retry flow smoke: ok")
