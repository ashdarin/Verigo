from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Protocol

from tencentcloud.common import credential
from tencentcloud.cloudstudio.v20230508 import cloudstudio_client, models

from app.config import settings
from app.db.jobs import JobStore, WorkerRuntime, job_store, utc_now


logger = logging.getLogger(__name__)
TENCENT_QQ_TARGET = "tencent_qq"
DOMESTIC_CLOUDSTUDIO_TARGET = "cloudstudio_domestic"


class WorkspaceApi(Protocol):
    def run_workspace(self) -> str: ...

    def stop_workspace(self) -> str: ...

    def workspace_status(self) -> str | None: ...


class TencentCloudStudioApi:
    """Typed control-plane adapter for the public Cloud Studio API."""

    def __init__(self, config: Any = settings) -> None:
        self.config = config
        credentials = credential.Credential(
            self.config.cloudstudio_secret_id, self.config.cloudstudio_secret_key
        )
        self._client = cloudstudio_client.CloudstudioClient(
            credentials, self.config.cloudstudio_region
        )

    def run_workspace(self) -> str:
        request = models.RunWorkspaceRequest()
        request.SpaceKey = self.config.cloudstudio_space_key
        response = self._client.RunWorkspace(request)
        return str(response.RequestId or "")

    def stop_workspace(self) -> str:
        request = models.StopWorkspaceRequest()
        request.SpaceKey = self.config.cloudstudio_space_key
        response = self._client.StopWorkspace(request)
        return str(response.RequestId or "")

    def workspace_status(self) -> str | None:
        request = models.DescribeWorkspacesRequest()
        response = self._client.DescribeWorkspaces(request)
        for workspace in response.Data or []:
            if workspace.SpaceKey == self.config.cloudstudio_space_key:
                return str(workspace.Status or "")
        return None


class WorkerLifecycleCoordinator:
    """Start and stop workspaces; the configured Start hook owns the worker."""

    def __init__(
        self,
        store: JobStore = job_store,
        api: WorkspaceApi | None = None,
        config: Any = settings,
        target: str = TENCENT_QQ_TARGET,
    ) -> None:
        self.store = store
        self._api = api
        self.config = config
        self.target = target
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def configured(self) -> bool:
        return bool(
            self.config.tencent_qq_worker_enabled
            and self.config.cloudstudio_lifecycle_enabled
            and self.config.cloudstudio_secret_id
            and self.config.cloudstudio_secret_key
            and self.config.cloudstudio_region
            and self.config.cloudstudio_space_key
        )

    def _workspace_api(self) -> WorkspaceApi:
        if self._api is None:
            self._api = TencentCloudStudioApi(self.config)
        return self._api

    def start(self) -> None:
        if not self.configured or (self._thread and self._thread.is_alive()):
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"cloudstudio-lifecycle-{self.target}", daemon=True
        )
        self._thread.start()
        logger.info("Cloud Studio lifecycle coordinator ready: target=%s", self.target)
        if self is globals().get("worker_lifecycle"):
            domestic_worker_lifecycle.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None
        if self is globals().get("worker_lifecycle"):
            domestic_worker_lifecycle.stop()

    def notify_job_queued(self) -> None:
        if self.configured:
            self._wake_event.set()

    def record_worker_seen(self, worker_id: str) -> None:
        self.store.record_worker_seen(self.target, worker_id)
        self._wake_event.set()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("Cloud Studio lifecycle reconciliation failed")
            self._wake_event.wait(self.config.cloudstudio_lifecycle_poll_seconds)
            self._wake_event.clear()

    def _is_online(self, runtime: WorkerRuntime, now: datetime) -> bool:
        return bool(
            runtime.last_seen_at
            and now - runtime.last_seen_at
            <= timedelta(seconds=self.config.cloudstudio_worker_online_seconds)
        )

    def _node_label(self) -> str:
        if self.target == DOMESTIC_CLOUDSTUDIO_TARGET:
            return "Cloud Studio domestic worker"
        return "Cloud Studio QQ worker"

    def _start_workspace(self, runtime: WorkerRuntime, now: datetime) -> None:
        if runtime.wake_requested_at:
            if runtime.wake_deadline_at and now >= runtime.wake_deadline_at:
                self.store.set_queued_target_message(
                    self.target, f"{self._node_label()} startup delayed; retrying"
                )
                self.store.clear_wake_state(self.target)
                runtime = self.store.worker_runtime(self.target)
            elif now < runtime.wake_requested_at + timedelta(
                seconds=self.config.cloudstudio_wake_retry_seconds
            ):
                return

        try:
            status = self._workspace_api().workspace_status()
        except Exception as exc:
            self.store.record_wake_attempt(self.target, deadline=None, error=str(exc)[:500])
            logger.warning("Cloud Studio status query failed: %s", exc)
            return

        deadline = now + timedelta(seconds=self.config.cloudstudio_startup_timeout_seconds)
        if status == "RUNNING":
            # The separate session service triggers the configured Start hook.
            self.store.record_wake_attempt(self.target, deadline=deadline, error=None)
            self.store.set_queued_target_message(
                self.target, f"{self._node_label()} is running; waiting for heartbeat"
            )
            return

        try:
            request_id = self._workspace_api().run_workspace()
        except Exception as exc:
            self.store.record_wake_attempt(self.target, deadline=None, error=str(exc)[:500])
            logger.warning("Cloud Studio RunWorkspace failed: %s", exc)
            return
        self.store.record_wake_attempt(self.target, deadline=deadline, error=None)
        self.store.set_queued_target_message(
            self.target, f"{self._node_label()} is starting"
        )
        logger.info("Cloud Studio RunWorkspace accepted: request_id=%s", request_id)

    def _stop_idle_workspace(self, runtime: WorkerRuntime, now: datetime) -> None:
        runtime = self.store.begin_worker_idle(self.target)
        if not runtime.idle_since or now - runtime.idle_since < timedelta(
            seconds=self.config.cloudstudio_idle_stop_seconds
        ):
            return
        if runtime.stop_requested_at and now < runtime.stop_requested_at + timedelta(
            seconds=self.config.cloudstudio_wake_retry_seconds
        ):
            return
        try:
            request_id = self._workspace_api().stop_workspace()
        except Exception as exc:
            self.store.record_stop_attempt(self.target, str(exc)[:500])
            logger.warning("Cloud Studio StopWorkspace failed: %s", exc)
            return
        self.store.record_stop_attempt(self.target, None)
        logger.info("Cloud Studio StopWorkspace accepted: request_id=%s", request_id)

    def tick(self, now: datetime | None = None) -> None:
        if not self.configured:
            return
        now = now or utc_now()
        active = self.store.active_target_count(self.target)
        runtime = self.store.worker_runtime(self.target)
        if active:
            self.store.clear_worker_idle(self.target)
            if self._is_online(runtime, now):
                self.store.clear_wake_state(self.target)
                self.store.set_queued_target_message(self.target, None)
            else:
                self._start_workspace(runtime, now)
            return

        self.store.clear_wake_state(self.target)
        if self._is_online(runtime, now):
            self._stop_idle_workspace(runtime, now)
        else:
            self.store.clear_worker_idle(self.target)


worker_lifecycle = WorkerLifecycleCoordinator()
secondary_cloudstudio_config = settings.secondary_cloudstudio_namespace()
domestic_worker_lifecycle = WorkerLifecycleCoordinator(
    config=secondary_cloudstudio_config,
    target=DOMESTIC_CLOUDSTUDIO_TARGET,
)
