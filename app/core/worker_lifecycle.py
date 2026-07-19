from __future__ import annotations

import logging
import os
import subprocess
import threading
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, Protocol

from tencentcloud.common import credential
from tencentcloud.cloudstudio.v20230508 import cloudstudio_client, models

from app.config import settings
from app.core.cloudstudio_startup import worker_start_command
from app.db.jobs import JobStore, WorkerRuntime, job_store, utc_now


logger = logging.getLogger(__name__)
TENCENT_QQ_TARGET = "tencent_qq"
RESTART_WAITING_FOR_STOP = "restart_waiting_for_workspace_stop"
SSH_BOOTSTRAP_COMPLETE = "ssh_bootstrap_complete"
IDE_SESSION_ACTIVATED = "ide_session_activated"


class WorkspaceApi(Protocol):
    def run_workspace(self) -> str: ...

    def stop_workspace(self) -> str: ...

    def workspace_status(self) -> str | None: ...

    def activate_workspace_session(self) -> None: ...


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

    def activate_workspace_session(self) -> None:
        """Run the IDE loader so Cloud Studio executes its Start lifecycle hook."""
        token_request = models.CreateWorkspaceTokenRequest()
        token_request.SpaceKey = settings.cloudstudio_space_key
        token_request.TokenExpiredLimitSec = 120
        token_request.Policies = ["all"]
        response = self._client.CreateWorkspaceToken(token_request)
        access_token = str(response.Token or "")
        if not access_token:
            raise RuntimeError("Cloud Studio returned an empty workspace access token")
        # RunWorkspace reaches RUNNING before the IDE frontend is consistently ready.
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/verigo/data/playwright")
        from playwright.sync_api import sync_playwright

        query = urllib.parse.urlencode({
            "token": access_token,
            "report_open_type": "vps_lifecycle",
        })
        url = (
            "https://ide.cloud.tencent.com/tty/"
            f"{settings.cloudstudio_space_key}/?{query}"
        )
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                try:
                    page = browser.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                    # The loader posts the token and establishes the remote sockets.
                    page.wait_for_timeout(20_000)
                finally:
                    browser.close()
        except Exception as exc:
            # Playwright errors may include the tokenized URL, so never expose them.
            raise RuntimeError("Cloud Studio IDE session activation failed") from exc

    def bootstrap_worker(self) -> None:
        """Run the idempotent QQ worker bootstrap through Cloud Studio SSH."""
        request = models.CreateWorkspaceTokenRequest()
        request.SpaceKey = settings.cloudstudio_space_key
        request.TokenExpiredLimitSec = settings.cloudstudio_ssh_token_expiry_seconds
        request.Policies = ["all"]
        response = self._client.CreateWorkspaceToken(request)
        access_token = str(response.Token or "")
        if not access_token:
            raise RuntimeError("Cloud Studio returned an empty workspace access token")

        remote = (
            f"{access_token}@{settings.cloudstudio_space_key}.ssh.cloudstudio.work"
        )
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={settings.cloudstudio_ssh_known_hosts_path}",
            "-o",
            "ConnectTimeout=20",
            "-i",
            str(settings.cloudstudio_ssh_key_path),
            remote,
            worker_start_command(),
        ]
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=90,
            check=False,
        )
        if result.returncode:
            # Never include the command here: it contains the temporary access token.
            detail = (
                result.stderr.replace(access_token, "[redacted]")
                .strip()
                .replace("\n", " ")[:300]
            )
            raise RuntimeError(
                f"Cloud Studio SSH bootstrap failed (exit {result.returncode}): {detail}"
            )


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

    @property
    def ssh_bootstrap_configured(self) -> bool:
        return bool(
            getattr(self.config, "cloudstudio_ssh_enabled", False)
            and getattr(self.config, "cloudstudio_ssh_key_path", None)
            and self.config.cloudstudio_ssh_key_path.is_file()
            and getattr(self.config, "cloudstudio_ssh_known_hosts_path", None)
            and self.config.cloudstudio_ssh_known_hosts_path.is_file()
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

    def _bootstrap_worker(
        self, runtime: WorkerRuntime, now: datetime, *, force: bool = False
    ) -> None:
        if not self.ssh_bootstrap_configured:
            return
        if runtime.last_wake_error == SSH_BOOTSTRAP_COMPLETE:
            return
        if not force and runtime.wake_requested_at and now < runtime.wake_requested_at + timedelta(
            seconds=self.config.cloudstudio_wake_retry_seconds
        ):
            return
        bootstrap = getattr(self._workspace_api(), "bootstrap_worker", None)
        if not callable(bootstrap):
            logger.error("Cloud Studio SSH bootstrap API is unavailable")
            return
        try:
            bootstrap()
        except Exception as exc:
            # Tencent's access token must never be logged, including via a command repr.
            detail = str(exc).replace("\n", " ")[:500]
            self.store.record_wake_attempt(
                TENCENT_QQ_TARGET, deadline=runtime.wake_deadline_at, error=detail
            )
            logger.warning("Cloud Studio SSH bootstrap failed: %s", detail)
            return
        self.store.record_wake_attempt(
            TENCENT_QQ_TARGET,
            deadline=runtime.wake_deadline_at,
            error=SSH_BOOTSTRAP_COMPLETE,
        )
        logger.info("Cloud Studio SSH worker bootstrap completed")

    def _activate_workspace_session(
        self, runtime: WorkerRuntime, now: datetime, *, force: bool = False
    ) -> None:
        if runtime.last_wake_error == IDE_SESSION_ACTIVATED:
            return
        if not force and runtime.wake_requested_at and now < runtime.wake_requested_at + timedelta(
            seconds=self.config.cloudstudio_wake_retry_seconds
        ):
            return
        activate = getattr(self._workspace_api(), "activate_workspace_session", None)
        if not callable(activate):
            logger.error("Cloud Studio IDE session activation API is unavailable")
            return
        try:
            activate()
        except Exception as exc:
            detail = str(exc).replace("\n", " ")[:500]
            self.store.record_wake_attempt(
                TENCENT_QQ_TARGET, deadline=runtime.wake_deadline_at, error=detail
            )
            logger.warning("Cloud Studio IDE session activation failed: %s", detail)
            return
        self.store.record_wake_attempt(
            TENCENT_QQ_TARGET,
            deadline=runtime.wake_deadline_at,
            error=IDE_SESSION_ACTIVATED,
        )
        logger.info("Cloud Studio IDE session activation completed")

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
            else:
                try:
                    status = self._workspace_api().workspace_status()
                except Exception as exc:
                    logger.warning("Could not check Cloud Studio startup status: %s", exc)
                    return
                if status == "RUNNING":
                    self._activate_workspace_session(
                        runtime, now, force=runtime.last_wake_error is None
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
            self.store.record_wake_attempt(
                TENCENT_QQ_TARGET, deadline=deadline, error=None
            )
            self.store.set_queued_target_message(
                TENCENT_QQ_TARGET, "Tencent QQ verification node is starting, please wait"
            )
            self._activate_workspace_session(
                self.store.worker_runtime(TENCENT_QQ_TARGET), now, force=True
            )
            return
            if self.ssh_bootstrap_configured:
                deadline = now + timedelta(
                    seconds=self.config.cloudstudio_startup_timeout_seconds
                )
                self.store.record_wake_attempt(
                    TENCENT_QQ_TARGET, deadline=deadline, error=None
                )
                self.store.set_queued_target_message(
                    TENCENT_QQ_TARGET, "Tencent QQ verification node is starting, please wait"
                )
                self._bootstrap_worker(
                    self.store.worker_runtime(TENCENT_QQ_TARGET), now, force=True
                )
                return
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
        self._activate_workspace_session(
            self.store.worker_runtime(TENCENT_QQ_TARGET), now, force=True
        )

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
