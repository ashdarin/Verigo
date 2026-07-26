from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from app.config import settings
from app.db.jobs import job_store

logger = logging.getLogger(__name__)
GMAIL_TARGET = "gmail"


class CloudShellLifecycle:
    def __init__(
        self,
        *,
        enabled: bool | None = None,
        user: str | None = None,
        quota_project: str | None = None,
        adc_path: Path | None = None,
        ssh_key_path: Path | None = None,
        ssh_known_hosts_path: Path | None = None,
        worker_id: str = "cloudshell-gmail-1",
        register_ssh_public_key: bool = False,
    ) -> None:
        self._enabled = settings.google_cloudshell_enabled if enabled is None else enabled
        self._user = settings.google_cloudshell_user if user is None else user
        self._quota_project = (
            settings.google_cloudshell_quota_project
            if quota_project is None
            else quota_project
        )
        self._adc_path = settings.google_cloudshell_adc_path if adc_path is None else adc_path
        self._ssh_key_path = (
            settings.google_cloudshell_ssh_key_path if ssh_key_path is None else ssh_key_path
        )
        self._ssh_known_hosts_path = (
            Path("/opt/verigo/data/cloudshell_known_hosts")
            if ssh_known_hosts_path is None
            else ssh_known_hosts_path
        )
        self._worker_id = worker_id
        self._register_ssh_public_key = register_ssh_public_key
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def configured(self) -> bool:
        return bool(
            settings.gmail_worker_enabled
            and self._enabled
            and settings.gmail_worker_token
            and self._user
            and self._quota_project
            and self._adc_path.is_file()
            and self._ssh_key_path.is_file()
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
        if self is globals().get("cloudshell_lifecycle"):
            cloudshell_secondary_lifecycle.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None
        if self is globals().get("cloudshell_lifecycle"):
            cloudshell_secondary_lifecycle.stop()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            if self.configured and job_store.active_target_count(GMAIL_TARGET):
                self.notify_job_queued()
            self._wake_event.wait(5)
            self._wake_event.clear()

    def record_worker_seen(self, worker_id: str) -> None:
        job_store.record_worker_seen(GMAIL_TARGET, worker_id)

    @staticmethod
    def _worker_command(release_version: str = "") -> str:
        """Replace a stale Gmail worker once; keep a matching worker alive."""
        version_check = (
            f" && tr '\\0' '\\n' < \"/proc/$(cat \"$pid_file\")/environ\" "
            f"| grep -qx 'VERIGO_REMOTE_WORKER_RELEASE={release_version}'"
            if release_version else ""
        )
        return (
            "cd ~/verigo-worker && python3 -m venv .venv && "
            ".venv/bin/pip -q install 'dnspython>=2.6,<3' && "
            "pid_file=.gmail-worker.pid; "
            "if test -s \"$pid_file\" && kill -0 \"$(cat \"$pid_file\")\" 2>/dev/null "
            "&& tr '\\0' '\\n' < \"/proc/$(cat \"$pid_file\")/environ\" "
            "| grep -qx 'VERIGO_REMOTE_WORKER_TARGET=gmail'"
            f"{version_check}; then exit 0; fi; "
            "if test -s \"$pid_file\"; then kill \"$(cat \"$pid_file\")\" 2>/dev/null || true; fi; "
            "pkill -TERM -f '[c]url.*workers/gmail' 2>/dev/null || true; sleep 1; "
            "rm -f \"$pid_file\"; "
            "(set -a; . .worker.env; set +a; nohup .venv/bin/python -m "
            "app.tencent_qq_worker >/tmp/verigo-gmail-worker.log 2>&1 </dev/null & "
            "echo $! > \"$pid_file\")"
        )

    def _token(self) -> str:
        credentials = json.loads(self._adc_path.read_text())
        data = urllib.parse.urlencode({
            "client_id": credentials["client_id"],
            "client_secret": credentials["client_secret"],
            "refresh_token": credentials["refresh_token"],
            "grant_type": "refresh_token",
        }).encode()
        with urllib.request.urlopen("https://oauth2.googleapis.com/token", data=data, timeout=30) as response:
            return str(json.load(response)["access_token"])

    @staticmethod
    def _operation_environment(operation: dict[str, object]) -> dict[str, object] | None:
        """Extract the environment when a Cloud Shell operation has completed."""
        error = operation.get("error")
        if isinstance(error, dict):
            raise RuntimeError(str(error.get("message") or "Cloud Shell start failed"))
        response = operation.get("response")
        if not isinstance(response, dict):
            return None
        environment = response.get("environment")
        return environment if isinstance(environment, dict) else None

    def _start_environment(self, token: str) -> dict[str, object]:
        user = urllib.parse.quote(self._user, safe="")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Goog-User-Project": self._quota_project,
        }
        request = urllib.request.Request(
            f"https://cloudshell.googleapis.com/v1/users/{user}/environments/default:start",
            data=json.dumps({"accessToken": token}).encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            operation = json.load(response)

        for _ in range(30):
            environment = self._operation_environment(operation)
            if environment is not None:
                return environment
            operation_name = str(operation.get("name") or "")
            if not operation_name:
                raise RuntimeError("Cloud Shell start returned no operation name")
            time.sleep(2)
            poll = urllib.request.Request(
                f"https://cloudshell.googleapis.com/v1/{operation_name}",
                headers=headers,
                method="GET",
            )
            with urllib.request.urlopen(poll, timeout=30) as response:
                operation = json.load(response)
        raise RuntimeError("Cloud Shell environment start timed out")

    @staticmethod
    def _cloudshell_public_key(value: str) -> str:
        """Cloud Shell's API accepts only the key type and Base64 payload."""
        parts = value.split()
        if len(parts) < 2 or parts[0] not in {
            "ssh-rsa",
            "ecdsa-sha2-nistp256",
            "ecdsa-sha2-nistp384",
            "ecdsa-sha2-nistp521",
        }:
            raise RuntimeError("Cloud Shell public key must use RSA or ECDSA")
        return " ".join(parts[:2])

    def _add_public_key(self, token: str) -> None:
        if not self._register_ssh_public_key:
            return
        public_key = subprocess.run(
            ["ssh-keygen", "-y", "-f", str(self._ssh_key_path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        data = json.dumps({"key": self._cloudshell_public_key(public_key)}).encode()
        user = urllib.parse.quote(self._user, safe="")
        request = urllib.request.Request(
            f"https://cloudshell.googleapis.com/v1/users/{user}/environments/default:addPublicKey",
            data=data,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-Goog-User-Project": self._quota_project,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30):
                pass
        except urllib.error.HTTPError as exc:
            if exc.code != 409:
                raise

    def _start(self) -> None:
        try:
            token = self._token()
            environment = self._start_environment(token)
            self._add_public_key(token)
            host, port = environment["sshHost"], str(environment["sshPort"])
            ssh_user = self._user.split("@", 1)[0]
            remote = f"{ssh_user}@{host}"
            base = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={self._ssh_known_hosts_path}", "-i", str(self._ssh_key_path), "-p", port, remote]
            # The Cloud Shell start operation can finish before its SSH tunnel
            # accepts connections. Wait for the actual worker transport instead
            # of treating the first refused connection as a node failure.
            for attempt in range(18):
                probe = subprocess.run(
                    base + ["true"], check=False, capture_output=True, timeout=15
                )
                if probe.returncode == 0:
                    break
                if attempt == 17:
                    detail = probe.stderr.decode(errors="replace").strip()
                    raise RuntimeError(f"Cloud Shell SSH did not become ready: {detail}")
                time.sleep(5)
            source_root = Path(__file__).resolve().parents[2]
            archive = subprocess.run(["tar", "-C", str(source_root), "-czf", "-", "app", "验证8.py"], check=True, capture_output=True).stdout
            subprocess.run(base + ["mkdir -p ~/verigo-worker && tar -xzf - -C ~/verigo-worker"], input=archive, check=True, timeout=90)
            release_version = Path("/opt/verigo/RELEASE_VERSION").read_text().strip()
            environment_file = "\n".join((
                "VERIGO_REMOTE_WORKER_TARGET=gmail",
                "VERIGO_REMOTE_WORKER_SERVER=https://verigo.site",
                f"VERIGO_REMOTE_WORKER_TOKEN={settings.gmail_worker_token}",
                f"VERIGO_TENCENT_QQ_WORKER_ID={self._worker_id}",
                f"VERIGO_CLOUDSHELL_MAX_WORKERS={settings.cloudshell_worker_max_workers}",
                f"VERIGO_REMOTE_WORKER_RELEASE={release_version}",
            )) + "\n"
            subprocess.run(base + ["cat > ~/verigo-worker/.worker.env && chmod 600 ~/verigo-worker/.worker.env"], input=environment_file.encode(), check=True, timeout=30)
            subprocess.run(base + [self._worker_command(release_version)], check=True, timeout=120)
            logger.info("Cloud Shell Gmail worker bootstrap completed")
        except Exception as exc:
            logger.exception("Cloud Shell Gmail worker bootstrap failed: %s", exc)
            job_store.set_queued_target_message(
                GMAIL_TARGET,
                "Gmail 验证节点启动失败，正在重试",
            )
        finally:
            self._lock.release()


cloudshell_lifecycle = CloudShellLifecycle()
cloudshell_secondary_lifecycle = CloudShellLifecycle(
    enabled=settings.google_cloudshell_secondary_enabled,
    user=settings.google_cloudshell_secondary_user,
    quota_project=settings.google_cloudshell_secondary_quota_project,
    adc_path=settings.google_cloudshell_secondary_adc_path,
    ssh_key_path=settings.google_cloudshell_secondary_ssh_key_path,
    ssh_known_hosts_path=settings.google_cloudshell_secondary_ssh_known_hosts_path,
    worker_id="cloudshell-gmail-2",
    register_ssh_public_key=True,
)
cloudshell_lifecycles = (cloudshell_lifecycle, cloudshell_secondary_lifecycle)


def notify_cloudshell_job_queued() -> None:
    for lifecycle in cloudshell_lifecycles:
        lifecycle.notify_job_queued()


def start_cloudshell_lifecycles() -> None:
    for lifecycle in cloudshell_lifecycles:
        lifecycle.start()


def stop_cloudshell_lifecycles() -> None:
    for lifecycle in reversed(cloudshell_lifecycles):
        lifecycle.stop()
