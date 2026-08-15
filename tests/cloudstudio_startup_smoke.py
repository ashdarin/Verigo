from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.cloudstudio_startup import (
    WORKER_START_COMMAND_NAME,
    worker_start_command,
    worker_start_script,
    workspace_configuration,
)
from app.core.verification_worker import timeout_result


script = worker_start_script()
assert "CloudStudio-Probe-Token" in script
assert "Verigo bootstrap failed" in script
assert "app.tencent_qq_worker" in script
assert "/api/workers/${VERIGO_REMOTE_WORKER_TARGET}/bundle" in script
assert "X-Verigo-Worker-Token" in script
assert "git clone" not in script
assert "git fetch" not in script
assert "verigo-qq-worker-watchdog.sh" in script
assert "verigo-qq-worker-watchdog.pid" in script
assert "nohup setsid" in script
assert "sleep 5" in script
assert "always replace the watchdogs" in script

command = worker_start_command()
assert command.startswith("bash -c 'curl -fsS") and command.endswith("\" | bash'")
assert "/api/workers/$VERIGO_REMOTE_WORKER_TARGET/bootstrap" in command
assert "${VERIGO_CLOUDSTUDIO_WORKER_PROCESSES:-1}" in command
assert len(command) < 512

settings = SimpleNamespace(
    tencent_qq_worker_token="worker-token",
    cloudstudio_probe_token="probe-token",
    cloudstudio_space_key="workspace-key",
)
lifecycle, envs = workspace_configuration(settings)
assert lifecycle.Start[0].Name == WORKER_START_COMMAND_NAME
assert lifecycle.Start[0].Command == command
assert {env.Name: env.Value for env in envs} == {
    "VERIGO_REMOTE_WORKER_TARGET": "tencent-qq",
    "VERIGO_REMOTE_WORKER_SERVER": "https://verigo.site",
    "VERIGO_REMOTE_WORKER_TOKEN": "worker-token",
    "VERIGO_TENCENT_QQ_SERVER": "https://verigo.site",
    "VERIGO_TENCENT_QQ_WORKER_TOKEN": "worker-token",
    "VERIGO_TENCENT_QQ_WORKER_ID": "cloudstudio-on-demand-qq",
    "VERIGO_CLOUDSTUDIO_WORKER_PROCESSES": "1",
    "VERIGO_QQ_WORKER_MAX_WORKERS": "6",
    "VERIGO_QQ_SMTP_PER_MX": "6",
    "VERIGO_EMAIL_HARD_TIMEOUT_SECONDS": "90",
    "VERIGO_DISPOSABLE_LOOKUP_ENABLED": "false",
    "VERIGO_DISPOSABLE_LOOKUP_URL": "https://disposable.debounce.io/",
    "VERIGO_DISPOSABLE_LOOKUP_TIMEOUT_SECONDS": "0.8",
    "VERIGO_DISPOSABLE_LOOKUP_BACKGROUND_WORKERS": "2",
    "VERIGO_DISPOSABLE_LOOKUP_BACKGROUND_QUEUE": "2",
    "VERIGO_DISPOSABLE_LOOKUP_POSITIVE_CACHE_HOURS": "720",
    "VERIGO_DISPOSABLE_LOOKUP_NEGATIVE_CACHE_HOURS": "168",
    "VERIGO_DISPOSABLE_LOOKUP_FAILURE_CACHE_SECONDS": "300",
    "VERIGO_TENCENT_QQ_POLL_SECONDS": "0.25",
    "VERIGO_TENCENT_QQ_RETRY_SECONDS": "5",
    "VERIGO_CLOUDSTUDIO_PROBE_TOKEN": "probe-token",
    "VERIGO_CLOUDSTUDIO_SPACE_KEY": "workspace-key",
}

_, domestic_envs = workspace_configuration(
    settings,
    worker_target="cloudstudio-domestic",
    worker_token="domestic-token",
    worker_id="cloudstudio-domestic-2",
)
assert {env.Name: env.Value for env in domestic_envs}["VERIGO_REMOTE_WORKER_TARGET"] == "cloudstudio-domestic"
assert {env.Name: env.Value for env in domestic_envs}["VERIGO_REMOTE_WORKER_TOKEN"] == "domestic-token"
assert {env.Name: env.Value for env in domestic_envs}["VERIGO_TENCENT_QQ_WORKER_ID"] == "cloudstudio-domestic-2"

multi_script = worker_start_script(3)
assert "for slot in 1 2 3" in multi_script
assert 'VERIGO_TENCENT_QQ_WORKER_ID="${base_worker_id}-${slot}"' in multi_script
assert "verigo-qq-worker-watchdog-${slot}.pid" in multi_script
multi_command = worker_start_command(3)
assert "${VERIGO_CLOUDSTUDIO_WORKER_PROCESSES:-3}" in multi_command

_, multi_envs = workspace_configuration(settings, worker_processes=3)
assert {env.Name: env.Value for env in multi_envs}["VERIGO_CLOUDSTUDIO_WORKER_PROCESSES"] == "3"
assert {env.Name: env.Value for env in multi_envs}["VERIGO_QQ_WORKER_MAX_WORKERS"] == "6"
assert {env.Name: env.Value for env in multi_envs}["VERIGO_QQ_SMTP_PER_MX"] == "6"

timed_out = timeout_result("slow@qq.com", 7)
assert timed_out["deliverable"] is None
assert timed_out["failure_reason"] == "smtp_timeout"
assert timed_out["retry_policy"] == "delayed"
assert timed_out["original_index"] == 7

print("cloudstudio startup smoke: ok")
