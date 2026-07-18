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
RESTART_WAITING_FOR_STOP = "restart_waiting_for_workspace_stop"


class WorkspaceApi(Protocol):
    def run_workspace(self) -> str: ...

    def stop_workspace(self) -> str: ...

    def workspace_status(self) -> str | None: ...


class TencentCloudStudioApi:
    """Small typed adapter around the official Cloud Studio API 3.0 SDK."""

    def __init__(self) -> None:
        credentials = credential.Credential(
            settings.cloudstudio_secret_id, settings.cloudstudio_secret_key
        )
        self._client = cloudstudio_client.CloudstudioClient(
            credentials, settings.cloudstudio_region
        )

    def run_workspace(self) -> str:
        request = models.RunWorkspaceRequest()
        request.SpaceKey = settings.cloudstudio_space_key
        response = self._client.RunWorkspace(request)
        return str(response.RequestId or "")

    def stop_workspace(self) -> str:
        request = models.StopWorkspaceRequest()
        request.SpaceKey = settings.cloudstudio_space_key
        response = self._client.StopWorkspace(request)
        return str(response.RequestId or "")

    def workspace_status(self) -> str | None:
        request = models.DescribeWorkspacesRequest()
        response = self._client.DescribeWorkspaces(request)
        for workspace in response.Data or []:
            if workspace.SpaceKey == settings.cloudstudio_space_key:
                return str(workspace.Status or "")
        return None


class WorkerLifecycleCoordinator:
    def __init__(
        self,
        store: JobStore = job_store,
        api: WorkspaceApi | None = None,
        config: Any = settings,
    ) -> None:
        self.store = store
        self._api = api
        self.config = config
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
            self._api = TencentCloudStudioApi()
        return self._api

    def start(self) -> None:
        if not self.configured or (self._thread and self._thread.is_alive()):
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="cloudstudio-lifecycle", daemon=True
        )
        self._thread.start()
        logger.info("Cloud Studio on-demand lifecycle coordinator is ready")

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None

    def notify_job_queued(self) -> None:
        if self.configured:
            self._wake_event.set()

    def record_worker_seen(self, worker_id: str) -> None:
        self.store.record_worker_seen(TENCENT_QQ_TARGET, worker_id)
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

    def _fail_waiting_jobs(self, message: str, stop_workspace: bool = False) -> None:
        failed = self.store.fail_queued_target(TENCENT_QQ_TARGET, message)
        self.store.clear_wake_state(TENCENT_QQ_TARGET)
        if failed:
            logger.error("Failed %s queued QQ jobs: %s", failed, message)
        if not stop_workspace:
            return
        try:
            request_id = self._workspace_api().stop_workspace()
        except Exception as exc:
            self.store.record_stop_attempt(TENCENT_QQ_TARGET, str(exc)[:500])
            logger.error("Cloud Studio StopWorkspace after startup timeout failed: %s", exc)
        else:
            self.store.record_stop_attempt(TENCENT_QQ_TARGET, None)
            logger.info(
                "Cloud Studio StopWorkspace after startup timeout accepted: request_id=%s",
                request_id,
            )

    def _wake_worker(self, runtime: WorkerRuntime, now: datetime) -> None:
        if runtime.wake_deadline_at:
            if now >= runtime.wake_deadline_at:
                self._fail_waiting_jobs(
                    "腾讯 QQ 验证节点启动超时，请稍后重新提交",
                    stop_workspace=True,
                )
            elif runtime.last_wake_error == RESTART_WAITING_FOR_STOP:
                try:
                    status = self._workspace_api().workspace_status()
                except Exception as exc:
                    logger.warning(
                        "Could not check Cloud Studio restart status: %s", exc
                    )
                    return
                if status == "STOPPED":
                    self.store.clear_wake_state(TENCENT_QQ_TARGET)
                    self._wake_worker(
                        self.store.worker_runtime(TENCENT_QQ_TARGET), now
                    )
            return

        if runtime.wake_requested_at:
            retry_at = runtime.wake_requested_at + timedelta(
                seconds=self.config.cloudstudio_wake_retry_seconds
            )
            if now < retry_at:
                return

        if runtime.wake_attempts >= self.config.cloudstudio_wake_max_attempts:
            self._fail_waiting_jobs("腾讯 QQ 验证节点启动失败，请稍后重新提交")
            return

        try:
            status = self._workspace_api().workspace_status()
        except Exception as exc:
            logger.warning("Could not check Cloud Studio status before wake: %s", exc)
            status = None

        if status == "RUNNING":
            deadline = now + timedelta(
                seconds=self.config.cloudstudio_startup_timeout_seconds
            )
            try:
                request_id = self._workspace_api().stop_workspace()
            except Exception as exc:
                logger.error("Cloud Studio restart StopWorkspace failed: %s", exc)
            else:
                self.store.record_wake_attempt(
                    TENCENT_QQ_TARGET,
                    deadline=deadline,
                    error=RESTART_WAITING_FOR_STOP,
                )
                self.store.set_queued_target_message(
                    TENCENT_QQ_TARGET, "腾讯 QQ 验证节点正在重启，请稍候"
                )
                logger.info(
                    "Cloud Studio restart requested for offline QQ worker: request_id=%s",
                    request_id,
                )
                return

        try:
            request_id = self._workspace_api().run_workspace()
        except Exception as exc:
            updated = self.store.record_wake_attempt(
                TENCENT_QQ_TARGET, deadline=None, error=str(exc)[:500]
            )
            logger.error("Cloud Studio RunWorkspace failed: %s", exc)
            if updated.wake_attempts >= self.config.cloudstudio_wake_max_attempts:
                self._fail_waiting_jobs("腾讯 QQ 验证节点启动失败，请稍后重新提交")
            else:
                self.store.set_queued_target_message(
                    TENCENT_QQ_TARGET,
                    f"腾讯 QQ 验证节点启动失败，正在重试（{updated.wake_attempts}/"
                    f"{self.config.cloudstudio_wake_max_attempts}）",
                )
            return

        deadline = now + timedelta(
            seconds=self.config.cloudstudio_startup_timeout_seconds
        )
        self.store.record_wake_attempt(TENCENT_QQ_TARGET, deadline=deadline, error=None)
        self.store.set_queued_target_message(
            TENCENT_QQ_TARGET, "腾讯 QQ 验证节点正在启动，请稍候"
        )
        logger.info("Cloud Studio RunWorkspace accepted: request_id=%s", request_id)

    def _stop_idle_worker(self, runtime: WorkerRuntime, now: datetime) -> None:
        runtime = self.store.begin_worker_idle(TENCENT_QQ_TARGET)
        if not runtime.idle_since:
            return
        if now - runtime.idle_since < timedelta(
            seconds=self.config.cloudstudio_idle_stop_seconds
        ):
            return
        if runtime.stop_requested_at:
            retry_at = runtime.stop_requested_at + timedelta(
                seconds=max(
                    self.config.cloudstudio_wake_retry_seconds,
                    self.config.cloudstudio_worker_online_seconds,
                )
            )
            if now < retry_at:
                return
        try:
            request_id = self._workspace_api().stop_workspace()
        except Exception as exc:
            self.store.record_stop_attempt(TENCENT_QQ_TARGET, str(exc)[:500])
            logger.error("Cloud Studio StopWorkspace failed: %s", exc)
            return
        self.store.record_stop_attempt(TENCENT_QQ_TARGET, None)
        logger.info("Cloud Studio StopWorkspace accepted: request_id=%s", request_id)

    def tick(self, now: datetime | None = None) -> None:
        if not self.configured:
            return
        now = now or utc_now()
        self.store.requeue_stale_jobs()
        active = self.store.active_target_count(TENCENT_QQ_TARGET)
        runtime = self.store.worker_runtime(TENCENT_QQ_TARGET)
        online = self._is_online(runtime, now)

        if active:
            self.store.clear_worker_idle(TENCENT_QQ_TARGET)
            if online:
                self.store.clear_wake_state(TENCENT_QQ_TARGET)
                self.store.set_queued_target_message(TENCENT_QQ_TARGET, None)
                return
            self._wake_worker(runtime, now)
            return

        self.store.clear_wake_state(TENCENT_QQ_TARGET)
        if online:
            self._stop_idle_worker(runtime, now)
        else:
            self.store.clear_worker_idle(TENCENT_QQ_TARGET)


worker_lifecycle = WorkerLifecycleCoordinator()
