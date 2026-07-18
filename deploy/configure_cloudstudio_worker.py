from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.core.cloudstudio_startup import workspace_configuration
from app.core.worker_lifecycle import TencentCloudStudioApi
from tencentcloud.cloudstudio.v20230508 import models


def main() -> None:
    if not all((
        settings.cloudstudio_secret_id,
        settings.cloudstudio_secret_key,
        settings.cloudstudio_region,
        settings.cloudstudio_space_key,
        settings.tencent_qq_worker_token,
        settings.cloudstudio_probe_token,
    )):
        raise SystemExit("Cloud Studio and QQ worker environment variables must be configured")

    lifecycle, envs = workspace_configuration(settings)
    request = models.ModifyWorkspaceRequest()
    request.SpaceKey = settings.cloudstudio_space_key
    request.Lifecycle = lifecycle
    request.Envs = envs
    response = TencentCloudStudioApi()._client.ModifyWorkspace(request)
    print(f"Cloud Studio worker lifecycle configured: request_id={response.RequestId}")


if __name__ == "__main__":
    main()
