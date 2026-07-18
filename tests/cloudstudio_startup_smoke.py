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


script = worker_start_script()
assert "CloudStudio-Probe-Token" in script
assert "app.tencent_qq_worker" in script
assert "[a]pp.tencent_qq_worker" in script
assert "nohup" in script

command = worker_start_command()
assert command.startswith("echo ") and command.endswith(" | base64 -d | bash")
encoded = command.removeprefix("echo ").removesuffix(" | base64 -d | bash")
assert base64.b64decode(encoded).decode() == script
assert "nohup" not in command

settings = SimpleNamespace(
    tencent_qq_worker_token="worker-token",
    cloudstudio_probe_token="probe-token",
    cloudstudio_space_key="workspace-key",
)
lifecycle, envs = workspace_configuration(settings)
assert lifecycle.Start[0].Name == WORKER_START_COMMAND_NAME
assert lifecycle.Start[0].Command == command
assert {env.Name: env.Value for env in envs} == {
    "VERIGO_TENCENT_QQ_SERVER": "https://verigo.site",
    "VERIGO_TENCENT_QQ_WORKER_TOKEN": "worker-token",
    "VERIGO_TENCENT_QQ_WORKER_ID": "cloudstudio-on-demand-qq",
    "VERIGO_TENCENT_QQ_POLL_SECONDS": "0.25",
    "VERIGO_TENCENT_QQ_RETRY_SECONDS": "5",
    "VERIGO_CLOUDSTUDIO_PROBE_TOKEN": "probe-token",
    "VERIGO_CLOUDSTUDIO_SPACE_KEY": "workspace-key",
}

print("cloudstudio startup smoke: ok")
