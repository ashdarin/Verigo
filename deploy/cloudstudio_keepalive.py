from __future__ import annotations

import concurrent.futures
import logging
import os
import time
import urllib.parse

from playwright.sync_api import sync_playwright
from tencentcloud.cloudstudio.v20230508 import models

from app.config import settings
from app.core.worker_lifecycle import TencentCloudStudioApi, secondary_cloudstudio_config


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SESSION_SECONDS = 43_200
RETRY_SECONDS = 15


def keep_workspace_active(label: str, config: object) -> None:
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/verigo/data/playwright")
    while True:
        try:
            api = TencentCloudStudioApi(config)
            if api.workspace_status() != "RUNNING":
                api.run_workspace()
                deadline = time.monotonic() + 300
                while api.workspace_status() != "RUNNING":
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Cloud Studio workspace did not reach RUNNING")
                    time.sleep(10)
            request = models.CreateWorkspaceTokenRequest()
            request.SpaceKey = config.cloudstudio_space_key
            request.TokenExpiredLimitSec = 900
            request.Policies = ["all"]
            token = str(api._client.CreateWorkspaceToken(request).Token or "")
            if not token:
                raise RuntimeError("Cloud Studio returned an empty workspace token")
            query = urllib.parse.urlencode({
                "token": token,
                "report_open_type": "vps_lifecycle_keepalive",
            })
            url = f"https://ide.cloud.tencent.com/tty/{config.cloudstudio_space_key}/?{query}"
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--disable-extensions",
                        "--disable-background-timer-throttling",
                        "--disable-renderer-backgrounding",
                    ],
                )
                try:
                    page = browser.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                    logger.info("%s Cloud Studio session connected", label)
                    page.wait_for_timeout(SESSION_SECONDS * 1000)
                finally:
                    browser.close()
        except Exception:
            logger.exception("%s Cloud Studio keepalive failed; retrying", label)
            time.sleep(RETRY_SECONDS)


def main() -> None:
    workspaces = []
    if settings.tencent_qq_worker_enabled and settings.cloudstudio_lifecycle_enabled:
        workspaces.append(("qq", settings))
    if (
        secondary_cloudstudio_config.tencent_qq_worker_enabled
        and secondary_cloudstudio_config.cloudstudio_lifecycle_enabled
    ):
        workspaces.append(("domestic", secondary_cloudstudio_config))
    if not workspaces:
        logger.info("No Cloud Studio workspace is enabled")
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(workspaces)) as executor:
        futures = [executor.submit(keep_workspace_active, *workspace) for workspace in workspaces]
        for future in futures:
            future.result()


if __name__ == "__main__":
    main()
