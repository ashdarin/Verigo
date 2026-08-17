from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings as base_settings
from app.core import smtp_cross_route
from app.db.jobs import Job
from app.tasks import verification


active_settings = replace(
    base_settings,
    smtp_cross_route_enabled=True,
    smtp_cross_route_shadow_mode=False,
    smtp_cross_route_target="local",
    smtp_cross_route_max_per_email=1,
    smtp_cross_route_concurrency=1,
    smtp_cross_route_per_mx_concurrency=1,
    smtp_cross_route_pressure_min_samples=5,
    smtp_cross_route_pressure_4xx_rate=0.60,
)
smtp_cross_route.settings = active_settings
verification.settings = active_settings


def temporary(email: str, *, mx: str | None = None, detail: str = "452 4.3.1 temporary") -> dict:
    result = {
        "email": email,
        "smtp_result": detail,
        "smtp_raw_result": detail,
        "smtp_code": detail.split(" ", 1)[0],
        "deliverable": None,
    }
    if mx:
        result["mx_records"] = [mx]
    return result


gmail = temporary("person@gmail.com", mx="aspmx.l.google.com")
decision = smtp_cross_route.decision_for(
    gmail["email"], gmail, source_target="gmail"
)
assert decision.eligible and decision.alternate_target == "local"
assert decision.reason == "isolated_temporary_4xx"

enterprise = temporary("person@company.example", mx="mx.company.example")
assert smtp_cross_route.decision_for(
    enterprise["email"], enterprise, source_target="codearts"
).eligible

qq = temporary("person@qq.com", mx="mx1.qq.com")
assert smtp_cross_route.decision_for(
    qq["email"], qq, source_target="tencent_qq"
).reason == "qq_source_excluded"

qq_source_non_qq = temporary("person@example.test", mx="mx.example.test")
assert smtp_cross_route.decision_for(
    qq_source_non_qq["email"], qq_source_non_qq, source_target="tencent_qq"
).reason == "qq_source_excluded"

qq_from_other_source = temporary("person@qq.com", mx="mx1.qq.com")
assert smtp_cross_route.decision_for(
    qq_from_other_source["email"], qq_from_other_source, source_target="codearts"
).reason == "qq_excluded"

microsoft_365 = temporary(
    "person@company.example", mx="tenant.mail.protection.outlook.com"
)
assert smtp_cross_route.decision_for(
    microsoft_365["email"], microsoft_365, source_target="codearts"
).reason == "microsoft_excluded"

greylist = temporary("person@example.test", detail="451 4.7.1 greylisted")
assert smtp_cross_route.decision_for(
    greylist["email"], greylist, source_target="codearts"
).reason == "greylist"

throttled = temporary(
    "person@example.test", detail="421 4.7.0 too many connections"
)
assert smtp_cross_route.decision_for(
    throttled["email"], throttled, source_target="codearts"
).reason == "receiver_throttled"

mailbox_full = temporary(
    "person@example.test", detail="452 4.2.2 mailbox over quota"
)
assert smtp_cross_route.decision_for(
    mailbox_full["email"], mailbox_full, source_target="codearts"
).reason == "mailbox_full"

pressure_batch = [
    temporary(f"person{index}@gmail.com", mx="aspmx.l.google.com")
    for index in range(5)
]
pressure = smtp_cross_route.provider_pressure_keys(pressure_batch)
assert pressure == {"gmail"}
assert smtp_cross_route.decision_for(
    pressure_batch[0]["email"],
    pressure_batch[0],
    source_target="gmail",
    pressure_keys=pressure,
).reason == "provider_pressure"


class FakeStore:
    def __init__(self, parent: Job) -> None:
        self.parent = parent
        self.added: list[Job] = []

    def upsert_results(self, job_id: str, results: list[dict]) -> None:
        assert job_id == self.parent.id

    def retry_children(self, parent_id: str) -> list[Job]:
        assert parent_id == self.parent.id
        return self.added

    def add(self, job: Job) -> None:
        self.added.append(job)

    def get(self, job_id: str) -> Job | None:
        return self.parent if job_id == self.parent.id else None

    def cache_results(self, results: list[dict], *, owner_job_id: str | None = None) -> list[str]:
        return []

    def complete_probe_leases(self, owner_job_id: str, results: list[dict]) -> list[str]:
        return []

    def record_catch_all(self, job: Job) -> None:
        pass


class FakeReviewEventStore:
    def __init__(self) -> None:
        self.events = []

    def record_many(self, events) -> bool:
        self.events.extend(events)
        return True


verification._notify_retry_target = lambda _job: None
verification.write_csv = lambda _job: None
verification.publish_completed_result_objects = lambda _job, _results=None: None
review_events = FakeReviewEventStore()
verification.smtp_review_event_store = review_events

parent = Job(
    id="cross-route-parent",
    emails=["person@gmail.com"],
    worker_count=8,
    status="completed",
    execution_target="gmail",
    results=[temporary("person@gmail.com", mx="aspmx.l.google.com")],
)
store = FakeStore(parent)
verification.job_store = store
verification.enqueue_background_retry(parent, parent, parent.emails, 1)
assert len(store.added) == 1
alternate = store.added[0]
assert alternate.execution_target == "local"
assert alternate.retry_route == "alternate_route"
assert alternate.origin_execution_target == "gmail"
assert alternate.worker_count == 1
assert alternate.temporary_retry_attempts == 0
assert alternate.cross_route_attempts == 1
assert parent.results[0]["cross_route_state"] == "scheduled"
assert [event.event_type for event in review_events.events] == ["scheduled"]
assert review_events.events[0].email_hash != "person@gmail.com"
verification.enqueue_background_retry(parent, parent, parent.emails, 1)
assert len(store.added) == 1
assert len(review_events.events) == 1

alternate.status = "completed"
alternate.results = [temporary("person@gmail.com", mx="aspmx.l.google.com")]
verification.finish_background_retry(alternate)
assert len(store.added) == 2
same_target = store.added[-1]
assert same_target.retry_route == "same_target"
assert same_target.execution_target == "gmail"
assert same_target.temporary_retry_attempts == 1
assert same_target.cross_route_attempts == 1
assert parent.results[0]["cross_route_state"] == "inconclusive"
assert [event.event_type for event in review_events.events] == [
    "scheduled", "completed", "excluded",
]
assert review_events.events[1].outcome == "inconclusive"

shadow_settings = replace(
    active_settings,
    smtp_cross_route_enabled=False,
    smtp_cross_route_shadow_mode=True,
)
smtp_cross_route.settings = shadow_settings
verification.settings = shadow_settings
shadow_parent = Job(
    id="cross-route-shadow-parent",
    emails=["person@company.example"],
    worker_count=4,
    status="completed",
    execution_target="codearts",
    results=[temporary("person@company.example", mx="mx.company.example")],
)
shadow_store = FakeStore(shadow_parent)
verification.job_store = shadow_store
verification.enqueue_background_retry(shadow_parent, shadow_parent, shadow_parent.emails, 1)
assert len(shadow_store.added) == 1
assert shadow_store.added[0].execution_target == "codearts"
assert shadow_store.added[0].retry_route == "same_target"
assert shadow_parent.results[0]["cross_route_state"] == "shadow_candidate"
assert review_events.events[-1].event_type == "shadow_candidate"

print("smtp cross-route smoke: ok")
