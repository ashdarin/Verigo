from __future__ import annotations

import hashlib
import json
import logging
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from app.config import settings
from app.db.jobs import job_store
from app.core.cloudshell_coordinator import cloudshell_coordinator

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
        worker_processes: int | None = None,
        register_ssh_public_key: bool = False,
        account_id: str | None = None,
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
        self._account_id = account_id or worker_id
        self._worker_processes = max(
            1,
            settings.cloudshell_worker_processes
            if worker_processes is None
            else worker_processes,
        )
        self._register_ssh_public_key = register_ssh_public_key
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure_count = 0
        self._retry_after = 0.0

    @property
    def account_id(self) -> str:
        return self._account_id

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
        if not cloudshell_coordinator.account_can_wake(self._account_id):
            return
        self._wake_event.set()
        if cloudshell_coordinator.worker_is_healthy(self._worker_id):
            return
        if time.monotonic() < self._retry_after:
            return
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
            try:
                if self.configured and (
                    job_store.active_target_count(GMAIL_TARGET)
                    or job_store.active_target_count("local")
                ):
                    self.notify_job_queued()
            except Exception:
                # A temporary database or provider failure must not permanently
                # stop the lifecycle thread. The next poll retries the wakeup.
                logger.exception(
                    "Cloud Shell lifecycle poll failed for account %s", self._account_id
                )
            self._wake_event.wait(5)
            self._wake_event.clear()

    def record_worker_seen(self, worker_id: str) -> None:
        job_store.record_worker_seen(GMAIL_TARGET, worker_id)

    @staticmethod
    def _worker_command(worker_number: int = 1, release_version: str = "") -> str:
        """Replace one stale worker process without disturbing its siblings."""
        if worker_number < 1:
            raise ValueError("Cloud Shell worker number must be positive")
        pid_file = f".gmail-worker-{worker_number}.pid"
        environment_file = f".gmail-worker-{worker_number}.env"
        release_marker = shlex.quote(f"VERIGO_REMOTE_WORKER_RELEASE={release_version}")
        version_check = (
            f" && tr '\\0' '\\n' < \"/proc/$(cat \"$pid_file\")/environ\" "
            f"| grep -qx {release_marker}"
            if release_version else ""
        )
        legacy_cleanup = (
            "if test -s .gmail-worker.pid; then "
            "kill \"$(cat .gmail-worker.pid)\" 2>/dev/null || true; fi; "
            "rm -f .gmail-worker.pid; "
            if worker_number == 1 else ""
        )
        dependency = "dnspython>=2.6,<3"
        dependency_hash = hashlib.sha256(dependency.encode()).hexdigest()
        dependency_marker = ".venv/.verigo-worker-deps-sha256"
        environment_bootstrap = (
            "if ! test -x .venv/bin/python; then python3 -m venv .venv; fi; "
            f"if ! test -s {dependency_marker} || "
            f"! grep -qx {shlex.quote(dependency_hash)} {dependency_marker}; then "
            f".venv/bin/pip -q install {shlex.quote(dependency)} && "
            f"printf '%s\\n' {shlex.quote(dependency_hash)} > {dependency_marker}; "
            "fi; "
        )
        return (
            "cd ~/verigo-worker && "
            f"{environment_bootstrap}"
            f"pid_file={pid_file}; environment_file={environment_file}; "
            f"{legacy_cleanup}"
            "if test -s \"$pid_file\" && kill -0 \"$(cat \"$pid_file\")\" 2>/dev/null "
            "&& tr '\\0' '\\n' < \"/proc/$(cat \"$pid_file\")/environ\" "
            "| grep -qx 'VERIGO_REMOTE_WORKER_TARGET=gmail'"
            f"{version_check}; then exit 0; fi; "
            "if test -s \"$pid_file\"; then kill \"$(cat \"$pid_file\")\" 2>/dev/null || true; fi; "
            "rm -f \"$pid_file\"; "
            "(set -a; . \"$environment_file\"; set +a; nohup .venv/bin/python -m "
            f"app.tencent_qq_worker >/tmp/verigo-gmail-worker-{worker_number}.log 2>&1 </dev/null & "
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

    def _refresh_host_key(self, host: str, port: str) -> None:
        """Refresh one recycled Cloud Shell endpoint without disabling host checks."""
        scan = subprocess.run(
            [
                "ssh-keyscan",
                "-T",
                "10",
                "-p",
                str(port),
                "-t",
                "ecdsa-sha2-nistp256",
                host,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if scan.returncode or not scan.stdout.strip():
            detail = scan.stderr.strip() or "Cloud Shell returned no ECDSA host key"
            raise RuntimeError(f"Cloud Shell host key refresh failed: {detail[:300]}")

        known_hosts = self._ssh_known_hosts_path
        known_hosts.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ssh-keygen", "-R", f"[{host}]:{port}", "-f", str(known_hosts)],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        with known_hosts.open("a", encoding="utf-8") as handle:
            handle.write(scan.stdout)
        try:
            known_hosts.chmod(0o600)
        except OSError:
            # Windows-based local smoke tests may not support POSIX permissions.
            pass

        fingerprint = subprocess.run(
            ["ssh-keygen", "-lf", "-"],
            input=scan.stdout,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.strip().split()
        logger.warning(
            "Refreshed Cloud Shell host key for %s:%s (fingerprint=%s)",
            host,
            port,
            fingerprint[1] if len(fingerprint) > 1 else "unknown",
        )

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
                detail = probe.stderr.decode(errors="replace")
                if "REMOTE HOST IDENTIFICATION HAS CHANGED" in detail:
                    self._refresh_host_key(host, port)
                    continue
                if attempt == 17:
                    raise RuntimeError(
                        f"Cloud Shell SSH did not become ready: {detail.strip()}"
                    )
                time.sleep(5)
            source_root = Path(__file__).resolve().parents[2]
            archive = subprocess.run(["tar", "-C", str(source_root), "-czf", "-", "app", "验证8.py"], check=True, capture_output=True).stdout
            subprocess.run(base + ["mkdir -p ~/verigo-worker && tar -xzf - -C ~/verigo-worker"], input=archive, check=True, timeout=90)
            release_version = Path("/opt/verigo/current/RELEASE_VERSION").read_text().strip()
            for worker_number in range(1, self._worker_processes + 1):
                environment_file = "\n".join((
                    "VERIGO_REMOTE_WORKER_TARGET=gmail",
                    "VERIGO_REMOTE_WORKER_SERVER=https://verigo.site",
                    f"VERIGO_REMOTE_WORKER_TOKEN={settings.gmail_worker_token}",
                    f"VERIGO_TENCENT_QQ_WORKER_ID={self._worker_id}-{worker_number}",
                    # The worker loop is single-lease. Its child verification
                    # parallelism remains controlled by the leased job.
                    "VERIGO_REMOTE_WORKER_CAPACITY=1",
                    f"VERIGO_REMOTE_WORKER_RELEASE={release_version}",
                )) + "\n"
                remote_path = f"~/verigo-worker/.gmail-worker-{worker_number}.env"
                subprocess.run(
                    base + [f"cat > {remote_path} && chmod 600 {remote_path}"],
                    input=environment_file.encode(),
                    check=True,
                    timeout=30,
                )
                subprocess.run(
                    base + [self._worker_command(worker_number, release_version)],
                    check=True,
                    timeout=120,
                )
            logger.info(
                "Cloud Shell Gmail worker bootstrap completed with %s processes",
                self._worker_processes,
            )
            self._failure_count = 0
            self._retry_after = 0.0
        except Exception as exc:
            self._failure_count += 1
            # Quota exhaustion can last for hours. Exponential backoff keeps a
            # depleted account out of the wake-up loop while other accounts
            # continue claiming Gmail work.
            delay = min(
                settings.cloudshell_quota_cooldown_seconds,
                300 * (2 ** min(self._failure_count - 1, 4)),
            )
            detail = str(exc).lower()
            if any(token in detail for token in ("quota", "resource_exhausted", "weekly", "rate limit")):
                delay = settings.cloudshell_quota_cooldown_seconds
            self._retry_after = time.monotonic() + delay
            cloudshell_coordinator.record_failure(self._worker_id, str(exc))
            logger.exception("Cloud Shell Gmail worker bootstrap failed: %s", exc)
            job_store.set_queued_target_message(
                GMAIL_TARGET,
                "Gmail 验证节点启动失败，正在重试",
            )
        finally:
            self._lock.release()


def _load_cloudshell_account_specs(path: Path) -> list[dict[str, Any]]:
    """Read extra account settings without ever accepting inline secrets."""
    if not str(path) or not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Cloud Shell account manifest could not be read: %s", exc)
        return []
    raw_accounts = payload.get("accounts") if isinstance(payload, dict) else payload
    if not isinstance(raw_accounts, list):
        logger.error("Cloud Shell account manifest must contain an accounts list")
        return []
    specs: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_accounts, start=3):
        if not isinstance(raw, dict):
            continue
        account_id = str(raw.get("id") or f"account{index}").strip()
        worker_id = str(raw.get("worker_id") or f"cloudshell-gmail-{account_id}").strip()
        required = ("user", "quota_project", "adc_path", "ssh_key_path")
        if not account_id or not worker_id or any(not str(raw.get(key) or "").strip() for key in required):
            logger.error("Skipping incomplete Cloud Shell account manifest entry %s", index)
            continue
        if any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in worker_id):
            logger.error("Skipping Cloud Shell account with unsafe worker id %s", worker_id)
            continue
        try:
            worker_processes = min(8, max(1, int(raw.get("worker_processes", 1))))
        except (TypeError, ValueError):
            logger.error("Skipping Cloud Shell account %s with invalid worker_processes", account_id)
            continue
        specs.append({
            "account_id": account_id,
            "worker_id": worker_id,
            "enabled": bool(raw.get("enabled", True)),
            "user": str(raw["user"]).strip(),
            "quota_project": str(raw["quota_project"]).strip(),
            "adc_path": Path(str(raw["adc_path"]).strip()),
            "ssh_key_path": Path(str(raw["ssh_key_path"]).strip()),
            "ssh_known_hosts_path": Path(str(raw.get("ssh_known_hosts_path") or f"/opt/verigo/data/{worker_id}_known_hosts").strip()),
            "worker_processes": worker_processes,
            "register_ssh_public_key": bool(raw.get("register_ssh_public_key", True)),
        })
    return specs


cloudshell_lifecycle = CloudShellLifecycle()
cloudshell_secondary_lifecycle = CloudShellLifecycle(
    enabled=settings.google_cloudshell_secondary_enabled,
    user=settings.google_cloudshell_secondary_user,
    quota_project=settings.google_cloudshell_secondary_quota_project,
    adc_path=settings.google_cloudshell_secondary_adc_path,
    ssh_key_path=settings.google_cloudshell_secondary_ssh_key_path,
    ssh_known_hosts_path=settings.google_cloudshell_secondary_ssh_known_hosts_path,
    worker_id="cloudshell-gmail-2",
    worker_processes=settings.cloudshell_secondary_worker_processes,
    register_ssh_public_key=True,
)
_configured_extra_lifecycles = tuple(
    CloudShellLifecycle(**spec)
    for spec in _load_cloudshell_account_specs(settings.google_cloudshell_accounts_file)
    if spec["worker_id"] not in {"cloudshell-gmail-1", "cloudshell-gmail-2"}
)
cloudshell_lifecycles = (
    cloudshell_lifecycle,
    cloudshell_secondary_lifecycle,
    *_configured_extra_lifecycles,
)


def notify_cloudshell_job_queued() -> None:
    for lifecycle in cloudshell_lifecycles:
        lifecycle.notify_job_queued()


def start_cloudshell_lifecycles() -> None:
    for lifecycle in cloudshell_lifecycles:
        lifecycle.start()


def stop_cloudshell_lifecycles() -> None:
    for lifecycle in reversed(cloudshell_lifecycles):
        lifecycle.stop()
