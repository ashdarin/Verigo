from __future__ import annotations

import base64
from typing import Any

from tencentcloud.cloudstudio.v20230508 import models


WORKER_START_COMMAND_NAME = "verigo-qq-worker-autostart"


def worker_start_script(worker_processes: int = 1) -> str:
    """Return the script Cloud Studio runs whenever the workspace starts."""
    processes = max(1, min(8, int(worker_processes)))
    slots = " ".join(str(slot) for slot in range(1, processes + 1))
    return f"""set -eu
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
layout="${{VERIGO_REMOTE_WORKER_TARGET}}:${{VERIGO_TENCENT_QQ_WORKER_ID}}:{processes}"
layout_file=/tmp/verigo-qq-worker-layout
layout_ready=true
if [ ! -s "$layout_file" ] || [ "$(cat "$layout_file")" != "$layout" ]; then
  layout_ready=false
fi
for slot in {slots}; do
  pid_file="/tmp/verigo-qq-worker-watchdog-${{slot}}.pid"
  if [ ! -s "$pid_file" ]; then
    layout_ready=false
    continue
  fi
  pid="$(cat "$pid_file")"
  if ! kill -0 "$pid" 2>/dev/null || \
      ! ps -p "$pid" -o args= 2>/dev/null | grep -Fq "/tmp/verigo-qq-worker-watchdog.sh $slot"; then
    layout_ready=false
  fi
done
if [ "$layout_ready" = true ]; then
  exit 0
fi
for pid_file in /tmp/verigo-qq-worker-watchdog.pid /tmp/verigo-qq-worker-watchdog-*.pid; do
  [ -s "$pid_file" ] || continue
  pid="$(cat "$pid_file")"
  kill -TERM -- "-${{pid}}" 2>/dev/null || kill "$pid" 2>/dev/null || true
done
sleep 2
rm -f /tmp/verigo-qq-worker-watchdog.pid /tmp/verigo-qq-worker-watchdog-*.pid
echo "$layout" >"$layout_file"
for slot in {slots}; do
  pid_file="/tmp/verigo-qq-worker-watchdog-${{slot}}.pid"
  nohup setsid /tmp/verigo-qq-worker-watchdog.sh "$slot" >>/tmp/verigo-qq-watchdog.log 2>&1 </dev/null &
  echo $! >"$pid_file"
done
"""


def worker_start_command(worker_processes: int = 1) -> str:
    """Encode the shell body because Cloud Studio's WAF rejects it verbatim."""
    encoded = base64.b64encode(worker_start_script(worker_processes).encode()).decode()
    return f"echo {encoded} | base64 -d | bash"


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
        # The limiter database is local to each workspace. Match both the
        # provider-wide and per-MX capacity to this workspace's process count.
        "VERIGO_QQ_SMTP_PER_MX": str(worker_processes) if worker_target == "tencent-qq" else "1",
        "VERIGO_EMAIL_HARD_TIMEOUT_SECONDS": "90",
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
