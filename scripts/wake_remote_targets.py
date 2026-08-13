#!/usr/bin/env python3
"""One-shot wake for tencent_qq (Cloud Studio) and gmail (Cloud Shell).

There is no request_wake(target) helper. record_wake_attempt() only writes
worker_runtime rows and does not start a workspace, so this script does not
call it.

In-process wake uses the same APIs as supervisor:

* tencent_qq: TencentCloudStudioApi.workspace_status / run_workspace /
  activate_workspace_session (requires Cloud Studio credentials in settings)
* gmail: CloudShellLifecycle.notify_job_queued (requires Cloud Shell ADC/SSH)

Does not print secrets, tokens, space keys, or exception text.

On the app host::

    cd /opt/verigo/current
    set -a; . /etc/verigo/verigo.env; set +a
    runuser -u verigo -- /opt/verigo/.venv/bin/python scripts/wake_remote_targets.py

If this process cannot load Cloud Studio / Cloud Shell credentials, use the
already-running supervisor instead of calling Tencent APIs from another box:

    worker_lifecycle.notify_job_queued()
    notify_cloudshell_job_queued()

Those only affect the supervisor process (in-memory wake events). Queued
tencent_qq / gmail / local jobs also make supervisor tick() wake on its poll.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_env_file() -> str | None:
    """Apply KEY=VALUE lines without printing values. Existing env wins."""
    candidates: list[Path] = []
    explicit = os.environ.get("VERIGO_ENV_FILE", "").strip()
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(Path("/etc/verigo/verigo.env"))
    for path in candidates:
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[7:].strip()
            if not key or key in os.environ:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ[key] = value
        return str(path)
    return None


_ENV_FILE = _load_env_file()

from app.config import settings  # noqa: E402
from app.core.cloudshell_coordinator import cloudshell_coordinator  # noqa: E402
from app.core.cloudshell_lifecycle import cloudshell_lifecycles  # noqa: E402
from app.core.worker_lifecycle import TencentCloudStudioApi, worker_lifecycle  # noqa: E402


def _say(target: str, action: str, **fields: object) -> None:
    parts = [f"target={target}", f"action={action}"]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        parts.append(f"{key}={value}")
    print(" ".join(parts), flush=True)


def _credentials_ready(config: object) -> bool:
    return bool(
        getattr(config, "cloudstudio_secret_id", None)
        and getattr(config, "cloudstudio_secret_key", None)
        and getattr(config, "cloudstudio_region", None)
        and getattr(config, "cloudstudio_space_key", None)
    )


def _wait_running(api: TencentCloudStudioApi, timeout_seconds: int) -> str | None:
    deadline = time.monotonic() + max(1, timeout_seconds)
    status: str | None = None
    while time.monotonic() < deadline:
        status = api.workspace_status()
        if status == "RUNNING":
            return status
        time.sleep(5)
    return status


def wake_tencent_qq() -> str:
    if not _credentials_ready(settings):
        _say(
            "tencent_qq",
            "skipped_not_configured",
            hint="source_/etc/verigo/verigo.env_then_TencentCloudStudioApi.run_workspace",
        )
        return "skipped"
    api = TencentCloudStudioApi(settings)
    try:
        status = api.workspace_status()
    except Exception:
        _say("tencent_qq", "status_failed")
        return "failed"
    if status == "RUNNING":
        action = "already_running"
        request_id = None
    else:
        try:
            request_id = api.run_workspace()
        except Exception:
            _say("tencent_qq", "run_workspace_failed", status=status)
            return "failed"
        action = "run_workspace"
        try:
            status = _wait_running(api, settings.cloudstudio_startup_timeout_seconds)
        except Exception:
            _say(
                "tencent_qq",
                "wait_failed",
                previous=action,
                request_id=request_id,
            )
            return "failed"
        if status != "RUNNING":
            _say(
                "tencent_qq",
                "startup_timeout",
                status=status,
                request_id=request_id,
            )
            return "failed"
    try:
        api.activate_workspace_session()
    except Exception:
        _say(
            "tencent_qq",
            "activate_failed",
            previous=action,
            status=status,
            request_id=request_id,
            configured=int(worker_lifecycle.configured),
        )
        return "failed"
    _say(
        "tencent_qq",
        "woke" if action == "run_workspace" else "already_running",
        status=status,
        request_id=request_id,
        session="activated",
    )
    return "ok"


def _wait_cloudshell(lifecycle: object, timeout_seconds: float) -> bool:
    lock = getattr(lifecycle, "_lock", None)
    if lock is None:
        return True
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if lock.acquire(blocking=False):
            lock.release()
            return True
        time.sleep(0.25)
    return False


def wake_gmail() -> str:
    configured = [item for item in cloudshell_lifecycles if item.configured]
    if not configured:
        _say(
            "gmail",
            "skipped_not_configured",
            hint="source_/etc/verigo/verigo.env_then_notify_cloudshell_job_queued",
        )
        return "skipped"
    try:
        cloudshell_coordinator.sync_accounts()
        cloudshell_coordinator.refresh_pool(force=True)
    except Exception:
        _say("gmail", "coordinator_refresh_failed")
        # Still try notify_job_queued; accounts that are already allowed can wake.
    results: list[str] = []
    for lifecycle in configured:
        worker_id = getattr(lifecycle, "_worker_id", lifecycle.account_id)
        try:
            healthy = cloudshell_coordinator.worker_is_healthy(worker_id)
        except Exception:
            healthy = False
        if healthy:
            _say("gmail", "already_healthy", worker_id=worker_id)
            results.append("ok")
            continue
        try:
            can_wake = cloudshell_coordinator.account_can_wake(lifecycle.account_id)
        except Exception:
            can_wake = True
        if not can_wake:
            _say("gmail", "skipped_coordinator_gate", worker_id=worker_id)
            results.append("skipped")
            continue
        lifecycle.notify_job_queued()
        lock = getattr(lifecycle, "_lock", None)
        started = bool(lock is not None and lock.locked())
        if started and not _wait_cloudshell(lifecycle, 420):
            _say("gmail", "bootstrap_timeout", worker_id=worker_id)
            results.append("failed")
            continue
        try:
            healthy = cloudshell_coordinator.worker_is_healthy(worker_id)
        except Exception:
            healthy = False
        if healthy:
            _say("gmail", "woke", worker_id=worker_id)
            results.append("ok")
        elif started:
            _say("gmail", "bootstrap_finished", worker_id=worker_id)
            results.append("ok")
        else:
            _say("gmail", "notify_noop", worker_id=worker_id)
            results.append("skipped")
    if any(item == "failed" for item in results):
        return "failed"
    if any(item == "ok" for item in results):
        return "ok"
    return "skipped"


def main() -> int:
    if _ENV_FILE:
        print(f"settings_source={_ENV_FILE}", flush=True)
    else:
        print("settings_source=process_environment", flush=True)
    outcomes = {
        "tencent_qq": wake_tencent_qq(),
        "gmail": wake_gmail(),
    }
    print(
        "summary "
        + " ".join(f"{name}={status}" for name, status in outcomes.items()),
        flush=True,
    )
    if all(status == "skipped" for status in outcomes.values()):
        print(
            "host_call=worker_lifecycle.notify_job_queued(); "
            "notify_cloudshell_job_queued()  # inside app.supervisor only",
            flush=True,
        )
        return 1
    if any(status == "failed" for status in outcomes.values()):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
