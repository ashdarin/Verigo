from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings as base_settings
from app.db import smtp_review_events as module


module.settings = replace(base_settings, metrics_salt="smtp-review-test-salt")


initial = {"smtp_code": "452", "smtp_result": "452 temporary"}
review = {"smtp_code": "250", "smtp_result": "250 accepted"}
event = module.make_smtp_review_event(
    parent_job_id="parent",
    retry_job_id="retry",
    email="Person@Example.test",
    provider_key="mx:mx.example.test",
    event_type="completed",
    decision_reason="isolated_temporary_4xx",
    origin_execution_target="gmail",
    review_execution_target="local",
    retry_route="alternate_route",
    attempt=1,
    initial_result=initial,
    review_result=review,
    outcome="confirmed_deliverable",
    latency_ms=1234,
)
duplicate = module.make_smtp_review_event(
    parent_job_id="parent",
    retry_job_id="retry",
    email="person@example.test",
    provider_key="mx:mx.example.test",
    event_type="completed",
    decision_reason="isolated_temporary_4xx",
    origin_execution_target="gmail",
    review_execution_target="local",
    retry_route="alternate_route",
    attempt=1,
    initial_result=initial,
    review_result=review,
    outcome="confirmed_deliverable",
)
assert event.id == duplicate.id
assert event.email_hash == duplicate.email_hash
assert event.email_hash != "person@example.test"
assert "person@example.test" not in repr(event).lower()
assert event.initial_smtp_code == "452"
assert event.review_smtp_code == "250"


class FakeConnection:
    def __init__(self) -> None:
        self.sql = ""
        self.rows = []
        self.committed = False

    def executemany(self, sql, rows):
        self.sql = " ".join(sql.split())
        self.rows = list(rows)

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        pass


connection = FakeConnection()
store = module.SMTPReviewEventStore()
store._connect = lambda: connection
assert store.record_many([event, duplicate])
assert len(connection.rows) == 2
assert "ON CONFLICT(id) DO NOTHING" in connection.sql
assert connection.committed
assert all("person@example.test" not in repr(row).lower() for row in connection.rows)

print("smtp review events smoke: ok")
