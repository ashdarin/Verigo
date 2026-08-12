from __future__ import annotations

import base64
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
assert "app.tencent_qq_worker" in script
assert "/api/workers/${VERIGO_REMOTE_WORKER_TARGET}/bundle" in script
assert "X-Verigo-Worker-Token" in script
assert "git clone" not in script
assert "git fetch" not in script
assert "verigo-qq-worker-watchdog" not in script
assert "verigo-qq-worker-${slot}.pid" in script
assert "kill -0" in script
assert "nohup setsid" in script
assert "env VERIGO_TENCENT_QQ_WORKER_ID" in script

command = worker_start_command()
assert command.startswith("echo ") and command.endswith(" | base64 -d | bash")
encoded = command.removeprefix("echo ").removesuffix(" | base64 -d | bash")
assert base64.b64decode(encoded).decode() == script
assert "setsid" not in command

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
    "VERIGO_QQ_SMTP_PER_MX": "1",
    "VERIGO_EMAIL_HARD_TIMEOUT_SECONDS": "90",
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
assert 'worker_id="${worker_id}-${slot}"' in multi_script
assert "verigo-qq-worker-${slot}.pid" in multi_script
multi_command = worker_start_command(3)
encoded = multi_command.removeprefix("echo ").removesuffix(" | base64 -d | bash")
assert base64.b64decode(encoded).decode() == multi_script

_, multi_envs = workspace_configuration(settings, worker_processes=3)
assert {env.Name: env.Value for env in multi_envs}["VERIGO_CLOUDSTUDIO_WORKER_PROCESSES"] == "3"
assert {env.Name: env.Value for env in multi_envs}["VERIGO_QQ_SMTP_PER_MX"] == "3"

timed_out = timeout_result("slow@qq.com", 7)
assert timed_out["deliverable"] is None
assert timed_out["failure_reason"] == "smtp_timeout"
assert timed_out["retry_policy"] == "delayed"
assert timed_out["original_index"] == 7

print("cloudstudio startup smoke: ok")
