from __future__ import annotations

import inspect
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace


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

    def get(self, job_id: str, include_results: bool = True) -> Job | None:
        return self.parent if job_id == self.parent.id else None

    def results_for_emails(self, _job_id: str, emails) -> list[dict]:
        wanted = {str(email).lower() for email in emails}
        return [
            dict(result) for result in self.parent.results
            if str(result.get("email", "")).lower() in wanted
        ]

    def initial_completion_times(self, _job_id: str, _emails) -> dict:
        return {}

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

# Unique-receiver runs must not queue multiple probes into one MX cooling bucket.
canary._active_emails = lambda: set()
canary._historical_4xx_rows = lambda **_kwargs: [
    {"email": "cooling@cooling.example.test", "result_json": {}},
    {"email": "first@first.example.test", "result_json": {}},
    {"email": "same-receiver@duplicate.example.test", "result_json": {}},
]
canary._stable_rows = lambda **_kwargs: [
    {"email": "stable@stable.example.test", "result_json": {"deliverable": True}},
]
canary.email_execution_target = lambda *_args, **_kwargs: "gmail"
canary.cross_route_decision = lambda *_args, **_kwargs: SimpleNamespace(eligible=True)
canary._receiver_keys = lambda emails: {
    "cooling.example.test": "mx-cooling",
    "first.example.test": "mx-a",
    "duplicate.example.test": "mx-a",
    "stable.example.test": "mx-b",
}
canary._cooling_receiver_keys = lambda _keys: {"mx-cooling"}
sample = canary.sample_candidates(
    total=2, fourxx=1, per_domain=1, lookback_days=90, seed="smoke",
    unique_receiver=True,
)
assert [candidate.cohort for candidate in sample] == ["historical_4xx", "stable"]
assert {candidate.email for candidate in sample} == {
    "first@first.example.test", "stable@stable.example.test",
}

print("smtp review canary smoke: ok")
