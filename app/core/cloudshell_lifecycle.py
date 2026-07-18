from __future__ import annotations

import json
import logging
import subprocess
import threading
import urllib.parse
import urllib.request
from pathlib import Path

from app.config import settings
from app.db.jobs import job_store

logger = logging.getLogger(__name__)
GMAIL_TARGET = "gmail"


class CloudShellLifecycle:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def configured(self) -> bool:
        return bool(
            settings.gmail_worker_enabled
            and settings.google_cloudshell_enabled
            and settings.gmail_worker_token
            and settings.google_cloudshell_user
            and settings.google_cloudshell_quota_project
            and settings.google_cloudshell_adc_path.is_file()
            and settings.google_cloudshell_ssh_key_path.is_file()
        )

    def notify_job_queued(self) -> None:
        self._wake_event.set()
        if not self.configured or not self._lock.acquire(blocking=False):
            return
        threading.Thread(target=self._start, name="cloudshell-gmail", daemon=True).start()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="cloudshell-gmail-lifecycle", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None

    def _run(self) -> None:
        while not self._stop_event.is_set():
            if self.configured and job_store.active_target_count(GMAIL_TARGET):
                self.notify_job_queued()
            self._wake_event.wait(5)
            self._wake_event.clear()

    def record_worker_seen(self, worker_id: str) -> None:
        job_store.record_worker_seen(GMAIL_TARGET, worker_id)

    @staticmethod
    def _worker_command() -> str:
        """Start exactly one Gmail worker without conflating it with a QQ worker."""
        return (
            "cd ~/verigo-worker && python3 -m venv .venv && "
            ".venv/bin/pip -q install 'dnspython>=2.6,<3' && "
            "if test -s .gmail-worker.pid && "
            "kill -0 \"$(cat .gmail-worker.pid)\" 2>/dev/null; then true; "
            "else nohup sh -c '. .worker.env; exec .venv/bin/python -m "
            "app.tencent_qq_worker' >/tmp/verigo-gmail-worker.log 2>&1 & "
            "echo $! > .gmail-worker.pid; fi"
        )

    def _token(self) -> str:
        credentials = json.loads(settings.google_cloudshell_adc_path.read_text())
        data = urllib.parse.urlencode({
            "client_id": credentials["client_id"],
            "client_secret": credentials["client_secret"],
            "refresh_token": credentials["refresh_token"],
            "grant_type": "refresh_token",
        }).encode()
        with urllib.request.urlopen("https://oauth2.googleapis.com/token", data=data, timeout=30) as response:
            return str(json.load(response)["access_token"])

    def _start(self) -> None:
        try:
            token = self._token()
            user = urllib.parse.quote(settings.google_cloudshell_user, safe="")
            request = urllib.request.Request(
                f"https://cloudshell.googleapis.com/v1/users/{user}/environments/default:start",
                data=json.dumps({"accessToken": token}).encode(),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "X-Goog-User-Project": settings.google_cloudshell_quota_project,
                }, method="POST",
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                environment = json.load(response)["response"]["environment"]
            host, port = environment["sshHost"], str(environment["sshPort"])
            ssh_user = settings.google_cloudshell_user.split("@", 1)[0]
            remote = f"{ssh_user}@{host}"
            base = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=/opt/verigo/data/cloudshell_known_hosts", "-i", str(settings.google_cloudshell_ssh_key_path), "-p", port, remote]
            source_root = Path(__file__).resolve().parents[2]
            archive = subprocess.run(["tar", "-C", str(source_root), "-czf", "-", "app", "验证8.py"], check=True, capture_output=True).stdout
            subprocess.run(base + ["mkdir -p ~/verigo-worker && tar -xzf - -C ~/verigo-worker"], input=archive, check=True, timeout=90)
            environment_file = "\n".join((
                "VERIGO_REMOTE_WORKER_TARGET=gmail",
                "VERIGO_REMOTE_WORKER_SERVER=https://verigo.site",
                f"VERIGO_REMOTE_WORKER_TOKEN={settings.gmail_worker_token}",
                "VERIGO_TENCENT_QQ_WORKER_ID=cloudshell-gmail-1",
            )) + "\n"
            subprocess.run(base + ["cat > ~/verigo-worker/.worker.env && chmod 600 ~/verigo-worker/.worker.env"], input=environment_file.encode(), check=True, timeout=30)
            subprocess.run(base + [self._worker_command()], check=True, timeout=120)
            logger.info("Cloud Shell Gmail worker bootstrap completed")
        except Exception as exc:
            logger.exception("Cloud Shell Gmail worker bootstrap failed: %s", exc)
            job_store.fail_queued_target(GMAIL_TARGET, "Gmail 验证节点启动失败，请稍后重新提交")
        finally:
            self._lock.release()


cloudshell_lifecycle = CloudShellLifecycle()
