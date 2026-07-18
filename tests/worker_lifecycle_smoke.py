from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.worker_lifecycle import (
    RESTART_WAITING_FOR_STOP,
    WorkerLifecycleCoordinator,
)
from app.db.jobs import WorkerRuntime


TARGET = "tencent_qq"


class FakeApi:
    def __init__(self) -> None:
        self.run_calls = 0
        self.stop_calls = 0
        self.fail_run = False
        self.status = "STOPPED"

    def run_workspace(self) -> str:
        self.run_calls += 1
        if self.fail_run:
            raise RuntimeError("simulated RunWorkspace failure")
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
        self.failed = 0

    def requeue_stale_jobs(self) -> int:
        return 0

    def active_target_count(self, target: str) -> int:
        assert target == TARGET
        return self.active

    def worker_runtime(self, target: str) -> WorkerRuntime:
        assert target == TARGET
        return self.runtime

    def set_queued_target_message(self, target: str, message: str | None) -> int:
        assert target == TARGET
        self.message = message
        return self.active

    def fail_queued_target(self, target: str, message: str) -> int:
        assert target == TARGET
        self.message = message
        self.failed += self.active
        self.active = 0
        return self.failed

    def record_wake_attempt(
        self, target: str, deadline: datetime | None, error: str | None
    ) -> WorkerRuntime:
        assert target == TARGET
        self.runtime = replace(
            self.runtime,
            wake_requested_at=current_time,
            wake_deadline_at=deadline,
            wake_attempts=self.runtime.wake_attempts + 1,
            last_wake_error=error,
        )
        return self.runtime

    def clear_wake_state(self, target: str) -> None:
        assert target == TARGET
        self.runtime = replace(
            self.runtime,
            wake_requested_at=None,
            wake_deadline_at=None,
            wake_attempts=0,
            last_wake_error=None,
        )

    def clear_worker_idle(self, target: str) -> None:
        assert target == TARGET
        self.runtime = replace(
            self.runtime,
            idle_since=None,
            stop_requested_at=None,
            last_stop_error=None,
        )

    def begin_worker_idle(self, target: str) -> WorkerRuntime:
        assert target == TARGET
        if self.runtime.idle_since is None:
            self.runtime = replace(self.runtime, idle_since=current_time)
        return self.runtime

    def record_stop_attempt(self, target: str, error: str | None) -> None:
        assert target == TARGET
        self.runtime = replace(
            self.runtime, stop_requested_at=current_time, last_stop_error=error
        )


config = SimpleNamespace(
    tencent_qq_worker_enabled=True,
    cloudstudio_lifecycle_enabled=True,
    cloudstudio_secret_id="secret-id",
    cloudstudio_secret_key="secret-key",
    cloudstudio_region="ap-guangzhou",
    cloudstudio_space_key="space-key",
    cloudstudio_worker_online_seconds=45,
    cloudstudio_startup_timeout_seconds=300,
    cloudstudio_wake_max_attempts=2,
    cloudstudio_wake_retry_seconds=15,
    cloudstudio_idle_stop_seconds=60,
    cloudstudio_lifecycle_poll_seconds=5,
)

current_time = datetime(2026, 7, 19, tzinfo=timezone.utc)
store = FakeStore()
api = FakeApi()
coordinator = WorkerLifecycleCoordinator(store=store, api=api, config=config)

store.active = 1
coordinator.tick(current_time)
assert api.run_calls == 1
assert store.message == "腾讯 QQ 验证节点正在启动，请稍候"
assert store.runtime.wake_deadline_at == current_time + timedelta(seconds=300)

store.runtime = replace(store.runtime, last_seen_at=current_time + timedelta(seconds=5))
current_time += timedelta(seconds=5)
coordinator.tick(current_time)
assert store.message is None
assert store.runtime.wake_deadline_at is None

timeout_store = FakeStore()
timeout_api = FakeApi()
timeout_coordinator = WorkerLifecycleCoordinator(
    store=timeout_store, api=timeout_api, config=config
)
timeout_store.active = 1
timeout_coordinator.tick(current_time)
current_time += timedelta(seconds=301)
timeout_coordinator.tick(current_time)
assert timeout_store.failed == 1
assert "启动超时" in str(timeout_store.message)
assert timeout_api.stop_calls == 1

restart_store = FakeStore()
restart_api = FakeApi()
restart_api.status = "RUNNING"
restart_coordinator = WorkerLifecycleCoordinator(
    store=restart_store, api=restart_api, config=config
)
restart_store.active = 1
restart_coordinator.tick(current_time)
assert restart_api.stop_calls == 1
assert restart_api.run_calls == 0
assert restart_store.runtime.last_wake_error == RESTART_WAITING_FOR_STOP
assert "正在重启" in str(restart_store.message)

current_time += timedelta(seconds=5)
restart_coordinator.tick(current_time)
assert restart_api.run_calls == 1
assert restart_store.runtime.last_wake_error is None

store.runtime = WorkerRuntime(target=TARGET)
api.fail_run = True
current_time += timedelta(seconds=60)
coordinator.tick(current_time)
assert api.run_calls == 2
assert "正在重试" in str(store.message)

current_time += timedelta(seconds=15)
coordinator.tick(current_time)
assert api.run_calls == 3
assert store.failed == 1
assert "启动失败" in str(store.message)

api.fail_run = False
store.runtime = WorkerRuntime(target=TARGET, last_seen_at=current_time)
current_time += timedelta(seconds=1)
coordinator.tick(current_time)
assert store.runtime.idle_since == current_time

current_time += timedelta(seconds=60)
store.runtime = replace(store.runtime, last_seen_at=current_time)
coordinator.tick(current_time)
assert api.stop_calls == 1
assert store.runtime.stop_requested_at == current_time

print("worker lifecycle smoke: ok")
