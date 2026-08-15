from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.jobs import Job
from app.tasks import verification


class FakeStore:
    def __init__(self, parent: Job) -> None:
        self.parent = parent
        self.added: list[Job] = []

    def get(self, job_id: str) -> Job | None:
        return self.parent if job_id == self.parent.id else None

    def persist(self, job: Job) -> None:
        assert job.id == self.parent.id

    def cache_results(self, results: list[dict]) -> None:
        assert results is self.parent.results

    def record_catch_all(self, job: Job) -> None:
        assert job.id == self.parent.id

    def retry_children(self, parent_id: str) -> list[Job]:
        assert parent_id == self.parent.id
        return self.added

    def add(self, job: Job) -> None:
        self.added.append(job)


verification.write_csv = lambda _job: None
verification.publish_completed_result_objects = lambda _job: None
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

print("smtp retry flow smoke: ok")
