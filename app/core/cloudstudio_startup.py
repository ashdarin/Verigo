from __future__ import annotations

from typing import Any

from tencentcloud.cloudstudio.v20230508 import models


WORKER_START_COMMAND_NAME = "verigo-qq-worker-autostart"


def worker_start_script(worker_processes: int = 1) -> str:
    """Return the script Cloud Studio runs whenever the workspace starts."""
    processes = max(1, min(8, int(worker_processes)))
    slots = " ".join(str(slot) for slot in range(1, processes + 1))
    return f"""set -eu
trap 'status=$?; printf "Verigo bootstrap failed: line=%s status=%s\\n" "$LINENO" "$status" >&2' ERR
curl -fsS --retry 3 --retry-delay 2 -X POST -H \"X-Verigo-CloudStudio-Probe-Token: ${{VERIGO_CLOUDSTUDIO_PROBE_TOKEN}}\" -H \"X-Verigo-CloudStudio-Workspace-Key: ${{VERIGO_CLOUDSTUDIO_SPACE_KEY}}\" https://verigo.site/api/workers/cloudstudio/probe >/tmp/verigo-cloudstudio-probe.log 2>&1 || true
mkdir -p /workspace/Verigo
bundle=/tmp/verigo-cloudstudio-worker.tar.gz
curl -fsS --retry 5 --retry-delay 2 --retry-connrefused \
  -H "X-Verigo-Worker-Token: ${{VERIGO_TENCENT_QQ_WORKER_TOKEN}}" \
  "https://verigo.site/api/workers/${{VERIGO_REMOTE_WORKER_TARGET}}/bundle" \
  -o "$bundle"
tar -xzf "$bundle" -C /workspace/Verigo
cd /workspace/Verigo
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv >/tmp/verigo-qq-venv.log 2>&1
fi
if [ ! -f .venv/.verigo-worker-deps ] || ! cmp -s requirements.txt .venv/.verigo-worker-deps; then
  .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt >/tmp/verigo-qq-pip.log 2>&1
  cp requirements.txt .venv/.verigo-worker-deps
fi
cat >/tmp/verigo-qq-worker-watchdog.sh <<'VERIGO_WATCHDOG'
#!/bin/sh
set -u
cd /workspace/Verigo
slot="${{1:?worker slot is required}}"
base_worker_id="${{VERIGO_TENCENT_QQ_WORKER_ID}}"
if [ "${{VERIGO_CLOUDSTUDIO_WORKER_PROCESSES:-1}}" -gt 1 ]; then
  export VERIGO_TENCENT_QQ_WORKER_ID="${{base_worker_id}}-${{slot}}"
fi
while true; do
  .venv/bin/python -m app.tencent_qq_worker >>/tmp/verigo-qq-worker.log 2>&1 || true
  sleep 5
done
VERIGO_WATCHDOG
chmod 700 /tmp/verigo-qq-worker-watchdog.sh
# Cloud Studio preserves workspace files across hibernation, including PID
# markers, even when a worker no longer has a usable network session. The Start
# lifecycle runs once per workspace start, so always replace the watchdogs.
for pid_file in /tmp/verigo-qq-worker-watchdog.pid /tmp/verigo-qq-worker-watchdog-*.pid; do
  [ -s "$pid_file" ] || continue
  pid="$(cat "$pid_file")"
  kill -TERM -- "-${{pid}}" 2>/dev/null || kill "$pid" 2>/dev/null || true
done
sleep 2
rm -f /tmp/verigo-qq-worker-watchdog.pid /tmp/verigo-qq-worker-watchdog-*.pid
for slot in {slots}; do
  pid_file="/tmp/verigo-qq-worker-watchdog-${{slot}}.pid"
  nohup setsid /tmp/verigo-qq-worker-watchdog.sh "$slot" >>/tmp/verigo-qq-watchdog.log 2>&1 </dev/null &
  echo $! >"$pid_file"
done
"""


def worker_start_command(worker_processes: int = 1) -> str:
    """Return a short lifecycle command accepted by Cloud Studio.

    Cloud Studio accepts the lifecycle update but silently skips commands around
    one kilobyte. The full bootstrap script is served by the worker API after
    token authentication, while this command stays small enough to execute.
    """
    processes = max(1, min(8, int(worker_processes)))
    return (
        "bash -c 'curl -fsS --retry 3 --retry-delay 2 "
        '-H "X-Verigo-Worker-Token: $VERIGO_TENCENT_QQ_WORKER_TOKEN" '
        '"https://verigo.site/api/workers/$VERIGO_REMOTE_WORKER_TARGET/'
        f"bootstrap?processes=${{VERIGO_CLOUDSTUDIO_WORKER_PROCESSES:-{processes}}}"
        "\" | bash'"
    )


def workspace_configuration(
    settings: Any,
    *,
    worker_target: str = "tencent-qq",
    worker_token: str | None = None,
    worker_id: str = "cloudstudio-on-demand-qq",
    worker_processes: int = 1,
) -> tuple[models.LifeCycle, list[models.Env]]:
    worker_processes = max(1, min(8, int(worker_processes)))
    lifecycle_command = models.LifeCycleCommand()
    lifecycle_command.Name = WORKER_START_COMMAND_NAME
    lifecycle_command.Command = worker_start_command(worker_processes)
    lifecycle = models.LifeCycle()
    lifecycle.Start = [lifecycle_command]

    token = worker_token or settings.tencent_qq_worker_token
    values = {
        "VERIGO_REMOTE_WORKER_TARGET": worker_target,
        "VERIGO_REMOTE_WORKER_SERVER": "https://verigo.site",
        "VERIGO_REMOTE_WORKER_TOKEN": token,
        # Keep the legacy names for the existing worker module and lifecycle hook.
        "VERIGO_TENCENT_QQ_SERVER": "https://verigo.site",
        "VERIGO_TENCENT_QQ_WORKER_TOKEN": token,
        "VERIGO_TENCENT_QQ_WORKER_ID": worker_id,
        "VERIGO_CLOUDSTUDIO_WORKER_PROCESSES": str(worker_processes),
        # All watchdogs in a workspace share this limiter database. The server
        # scheduler separately keeps aggregate QQ pressure within the same cap.
        "VERIGO_QQ_WORKER_MAX_WORKERS": "6",
        "VERIGO_QQ_SMTP_PER_MX": "6" if worker_target == "tencent-qq" else "1",
        "VERIGO_EMAIL_HARD_TIMEOUT_SECONDS": "90",
        # The public lookup receives only probe@domain. Cloud Studio gets no
        # database credentials, so it keeps an isolated process cache.
        "VERIGO_DISPOSABLE_LOOKUP_ENABLED": "true" if getattr(settings, "disposable_lookup_enabled", False) else "false",
        "VERIGO_DISPOSABLE_LOOKUP_URL": str(getattr(settings, "disposable_lookup_url", "https://disposable.debounce.io/")),
        "VERIGO_DISPOSABLE_LOOKUP_TIMEOUT_SECONDS": str(getattr(settings, "disposable_lookup_timeout_seconds", 0.8)),
        "VERIGO_DISPOSABLE_LOOKUP_BACKGROUND_WORKERS": str(getattr(settings, "disposable_lookup_background_workers", 2)),
        "VERIGO_DISPOSABLE_LOOKUP_BACKGROUND_QUEUE": str(getattr(settings, "disposable_lookup_background_queue", 2)),
        "VERIGO_DISPOSABLE_LOOKUP_POSITIVE_CACHE_HOURS": str(getattr(settings, "disposable_lookup_positive_cache_hours", 720)),
        "VERIGO_DISPOSABLE_LOOKUP_NEGATIVE_CACHE_HOURS": str(getattr(settings, "disposable_lookup_negative_cache_hours", 168)),
        "VERIGO_DISPOSABLE_LOOKUP_FAILURE_CACHE_SECONDS": str(getattr(settings, "disposable_lookup_failure_cache_seconds", 300)),
        "VERIGO_TENCENT_QQ_POLL_SECONDS": "0.25",
        "VERIGO_TENCENT_QQ_RETRY_SECONDS": "5",
        "VERIGO_CLOUDSTUDIO_PROBE_TOKEN": settings.cloudstudio_probe_token,
        "VERIGO_CLOUDSTUDIO_SPACE_KEY": settings.cloudstudio_space_key,
    }
    envs: list[models.Env] = []
    for name, value in values.items():
        env = models.Env()
        env.Name = name
        env.Value = value
        envs.append(env)
    return lifecycle, envs
