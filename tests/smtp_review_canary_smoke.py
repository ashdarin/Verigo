from __future__ import annotations

import inspect
import sys
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings as base_settings
from app.core import smtp_cross_route
from app.db.jobs import Job
from app.tasks import verification
from scripts import verification_history_canary as canary


active_settings = replace(
    base_settings,
    smtp_cross_route_enabled=True,
    smtp_cross_route_shadow_mode=False,
    smtp_cross_route_target="local",
    smtp_cross_route_max_per_email=1,
)
verification.settings = active_settings
smtp_cross_route.settings = active_settings


class FakeStore:
    def __init__(self, parent: Job) -> None:
        self.parent = parent
        self.added: list[Job] = []

    def persist(self, _job: Job) -> None:
        pass

    def cache_results(self, _results, *, owner_job_id=None):
        return []

    def complete_probe_leases(self, _owner_job_id, _results):
        return []

    def retry_children(self, _parent_id):
        return self.added

    def upsert_results(self, _job_id, _results):
        pass

    def add(self, job: Job):
        self.added.append(job)


class FakeEvents:
    def record_many(self, _events):
        return True


verification._notify_retry_target = lambda _job: None
verification.smtp_review_event_store = FakeEvents()
verification.publish_completed_result_objects = lambda *_args, **_kwargs: None
verification.write_csv = lambda *_args, **_kwargs: None


def temporary_job(list_name: str) -> Job:
    return Job(
        id=f"canary-{len(list_name)}",
        emails=["person@gmail.com"],
        worker_count=1,
        execution_target="gmail",
        list_name=list_name,
        is_cache_refresh=True,
        results=[{
            "email": "person@gmail.com",
            "smtp_code": "452",
            "smtp_result": "452 4.3.1 temporary recipient failure",
            "deliverable": None,
            "progress_state": "completed",
        }],
    )


ordinary = temporary_job("__verification_cache_refresh__")
ordinary_store = FakeStore(ordinary)
verification.job_store = ordinary_store
verification.finish_initial_job(ordinary)
assert ordinary_store.added == []

internal = temporary_job(verification.SMTP_REVIEW_CANARY_LIST_NAME)
internal_store = FakeStore(internal)
verification.job_store = internal_store
verification.finish_initial_job(internal)
assert len(internal_store.added) == 1
retry = internal_store.added[0]
assert retry.retry_route == "alternate_route"
assert retry.is_cache_refresh is True
assert retry.list_name == verification.SMTP_REVIEW_CANARY_LIST_NAME

assert canary._excluded("person@qq.com")
assert canary._excluded("person@tenant.outlook.com")
assert canary._excluded("person@yahoo.co.jp")
assert not canary._excluded("person@company.example")
assert "print(email" not in inspect.getsource(canary)
assert "is_cache_refresh=True" in inspect.getsource(canary.main)
assert "--confirm-production" in inspect.getsource(canary.main)
assert "deferred_retry_at IS NULL" in inspect.getsource(canary._active_user_jobs)

print("smtp review canary smoke: ok")
