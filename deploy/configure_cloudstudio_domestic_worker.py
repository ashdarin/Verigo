from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.core.cloudstudio_startup import workspace_configuration
from app.core.worker_lifecycle import (
    DOMESTIC_CLOUDSTUDIO_TARGET,
    TencentCloudStudioApi,
    secondary_cloudstudio_config,
)
from tencentcloud.cloudstudio.v20230508 import models


def main() -> None:
    if not all((
        secondary_cloudstudio_config.cloudstudio_secret_id,
        secondary_cloudstudio_config.cloudstudio_secret_key,
        secondary_cloudstudio_config.cloudstudio_region,
        secondary_cloudstudio_config.cloudstudio_space_key,
        settings.cloudstudio_domestic_worker_token,
        settings.cloudstudio_probe_token,
    )):
        raise SystemExit("Secondary Cloud Studio domestic worker variables are required")

    lifecycle, envs = workspace_configuration(
        secondary_cloudstudio_config,
        worker_target="cloudstudio-domestic",
        worker_token=settings.cloudstudio_domestic_worker_token,
        worker_id="cloudstudio-domestic-2",
    )
    request = models.ModifyWorkspaceRequest()
    request.SpaceKey = secondary_cloudstudio_config.cloudstudio_space_key
    request.Lifecycle = lifecycle
    request.Envs = envs
    repository = models.GitRepository()
    repository.Url = "https://github.com/ashdarin/Verigo.git"
    repository.Branch = "main"
    request.Repository = repository
    response = TencentCloudStudioApi(secondary_cloudstudio_config)._client.ModifyWorkspace(
        request
    )
    print(f"Cloud Studio domestic worker configured: request_id={response.RequestId}")


if __name__ == "__main__":
    main()
