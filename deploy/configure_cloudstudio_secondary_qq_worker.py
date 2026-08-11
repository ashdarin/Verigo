from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.core.cloudstudio_startup import workspace_configuration
from app.core.worker_lifecycle import TencentCloudStudioApi, secondary_cloudstudio_config
from tencentcloud.cloudstudio.v20230508 import models


def main() -> None:
    if not all((
        secondary_cloudstudio_config.cloudstudio_secret_id,
        secondary_cloudstudio_config.cloudstudio_secret_key,
        secondary_cloudstudio_config.cloudstudio_region,
        secondary_cloudstudio_config.cloudstudio_space_key,
        settings.tencent_qq_worker_token,
        settings.cloudstudio_probe_token,
    )):
        raise SystemExit("Secondary Cloud Studio and QQ worker variables are required")

    lifecycle, envs = workspace_configuration(
        secondary_cloudstudio_config,
        worker_target="tencent-qq",
        worker_token=settings.tencent_qq_worker_token,
        worker_id="cloudstudio-qq-2",
        worker_processes=2,
    )
    request = models.ModifyWorkspaceRequest()
    request.SpaceKey = secondary_cloudstudio_config.cloudstudio_space_key
    request.Lifecycle = lifecycle
    request.Envs = envs
    response = TencentCloudStudioApi(secondary_cloudstudio_config)._client.ModifyWorkspace(
        request
    )
    print(f"Secondary Cloud Studio QQ worker configured: request_id={response.RequestId}")


if __name__ == "__main__":
    main()
