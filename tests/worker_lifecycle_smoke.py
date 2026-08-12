from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.worker_lifecycle import WorkerLifecycleCoordinator
from app.db.jobs import WorkerRuntime


TARGET = "tencent_qq"
now = datetime(2026, 7, 19, tzinfo=timezone.utc)


class FakeApi:
    def __init__(self, status: str = "STOPPED") -> None:
        self.status = status
        self.run_calls = 0
        self.stop_calls = 0

    def run_workspace(self) -> str:
        self.run_calls += 1
        self.status = "RUNNING"
        return f"run-{self.run_calls}"

    def stop_workspace(self) -> str:
        self.stop_calls += 1
        self.status = "STOPPED"
        return f"stop-{self.stop_calls}"

    def workspace_status(self) -> str:
        return self.status


class FakeStore:
    def __init__(self) -> None:
        self.active = 0
        self.runtime = WorkerRuntime(target=TARGET)
        self.message: str | None = None

    def active_target_count(self, target: str) -> int:
        assert target == TARGET
        return self.active

    def worker_runtime(self, target: str) -> WorkerRuntime:
        assert target == TARGET
        return self.runtime

    def record_wake_attempt(self, target, deadline, error):
        self.runtime = replace(
            self.runtime,
            wake_requested_at=now,
            wake_deadline_at=deadline,
            wake_attempts=self.runtime.wake_attempts + 1,
            last_wake_error=error,
        )
        return self.runtime

    def clear_wake_state(self, target):
        self.runtime = replace(
            self.runtime, wake_requested_at=None, wake_deadline_at=None,
            wake_attempts=0, last_wake_error=None,
        )

    def set_queued_target_message(self, target, message):
        self.message = message
        return self.active

    def clear_worker_idle(self, target):
        self.runtime = replace(self.runtime, idle_since=None, stop_requested_at=None)

    def begin_worker_idle(self, target):
        if self.runtime.idle_since is None:
            self.runtime = replace(self.runtime, idle_since=now)
        return self.runtime

    def record_stop_attempt(self, target, error):
        self.runtime = replace(self.runtime, stop_requested_at=now, last_stop_error=error)


config = SimpleNamespace(
    tencent_qq_worker_enabled=True,
    cloudstudio_lifecycle_enabled=True,
    cloudstudio_secret_id="secret-id",
    cloudstudio_secret_key="secret-key",
    cloudstudio_region="ap-beijing",
    cloudstudio_space_key="space-key",
    cloudstudio_worker_online_seconds=45,
    cloudstudio_startup_timeout_seconds=300,
    cloudstudio_wake_retry_seconds=15,
    cloudstudio_idle_stop_seconds=60,
    cloudstudio_lifecycle_poll_seconds=5,
)

store = FakeStore()
api = FakeApi()
coordinator = WorkerLifecycleCoordinator(store=store, api=api, config=config)
store.active = 1
coordinator.tick(now)
assert api.run_calls == 1
assert store.runtime.wake_deadline_at == now + timedelta(seconds=300)

# A RUNNING workspace waits for the Start lifecycle hook rather than trying
# SSH or launching an IDE session from the queue supervisor.
running_store = FakeStore()
running_store.active = 1
running_api = FakeApi(status="RUNNING")
running = WorkerLifecycleCoordinator(store=running_store, api=running_api, config=config)
running.tick(now)
assert running_api.run_calls == 0
assert running_store.runtime.wake_deadline_at is not None

# A worker heartbeat clears startup state.
running_store.runtime = replace(running_store.runtime, last_seen_at=now)
running.tick(now)
assert running_store.runtime.wake_deadline_at is None
assert running_store.message is None

# Idle workers are stopped after the configured delay.
idle_store = FakeStore()
idle_api = FakeApi(status="RUNNING")
idle = WorkerLifecycleCoordinator(store=idle_store, api=idle_api, config=config)
idle_store.runtime = WorkerRuntime(target=TARGET, last_seen_at=now)
idle.tick(now)
assert idle_store.runtime.idle_since == now
idle_store.runtime = replace(idle_store.runtime, last_seen_at=now + timedelta(seconds=60))
idle.tick(now + timedelta(seconds=60))
assert idle_api.stop_calls == 1

print("worker lifecycle smoke: ok")
