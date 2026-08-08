"""Run the batch coordinator with synchronous fakes and no SMTP/network access."""

from __future__ import annotations

from queue import Queue
from threading import RLock
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.verification_batch import VerificationBatchRunner
from app.core.verification_worker import run_verification_worker


class FakeVerifier:
    def __init__(self) -> None:
        self.dns_cache: dict[str, object] = {}
        self.consumer_domains = {"example.com"}
        self.consumer_fix_strategies: dict[str, object] = {}

    def is_consumer_fix_supported(self, _domain: str) -> bool:
        return False

    def get_consumer_fix_strategy(self, _domain: str):
        return None

    def verify_email_comprehensive(self, email: str, process_id: int) -> dict[str, object]:
        return {"email": email, "process_id": process_id, "strategy": "fast"}


class InlineProcess:
    def __init__(self, *, target, args) -> None:
        self.target = target
        self.args = args
        self.started = False
        self.terminated = False

    def start(self) -> None:
        self.started = True
        self.target(*self.args)

    def join(self, timeout=None) -> None:
        return None

    def is_alive(self) -> bool:
        return False

    def terminate(self) -> None:
        self.terminated = True


class NoopProcess(InlineProcess):
    def start(self) -> None:
        self.started = True


class FakeManager:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def dict(self):
        return {}

    def RLock(self):
        return RLock()

    def shutdown(self) -> None:
        self.events.append("manager.shutdown")


class Controller:
    def __init__(self) -> None:
        self.user_max_processes = 8
        self.process_stats: dict[int, dict[str, object]] = {}
        self.results: list[dict[str, object]] = []

    def _clean_email_list(self, emails):
        return list(emails)


events: list[str] = []
controller = Controller()
runner = VerificationBatchRunner(
    controller,
    process_factory=InlineProcess,
    queue_factory=Queue,
    manager_factory=lambda: FakeManager(events),
    verifier_factory=FakeVerifier,
    cache_saver=lambda: events.append("cache.save"),
    sleeper=lambda _delay: None,
)
callbacks: list[str] = []
results = runner.run(
    ["third@example.com", "first@example.com", "second@example.com"],
    num_processes=1,
    result_callback=lambda result: callbacks.append(result["email"]),
)
assert [result["email"] for result in results] == [
    "third@example.com",
    "first@example.com",
    "second@example.com",
]
assert callbacks == ["third@example.com", "first@example.com", "second@example.com"]
assert controller.results == results
assert events == ["cache.save"]
assert controller.process_stats[1]["status"] == "completed"

stop_events: list[str] = []
stop_controller = Controller()
stop_runner = VerificationBatchRunner(
    stop_controller,
    process_factory=NoopProcess,
    queue_factory=Queue,
    manager_factory=lambda: FakeManager(stop_events),
    verifier_factory=FakeVerifier,
    cache_saver=lambda: stop_events.append("cache.save"),
    clock=lambda: 1.0,
    sleeper=lambda _delay: None,
)
assert stop_runner.run(["stop@example.com"], num_processes=1, should_stop=lambda: True) == []
assert stop_events == ["cache.save"]

print("verification batch smoke: ok")
