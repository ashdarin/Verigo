from __future__ import annotations

from pathlib import Path
import json
import tempfile
import sys
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.cloudshell_lifecycle import CloudShellLifecycle, _load_cloudshell_account_specs


command = CloudShellLifecycle._worker_command(2, "release-123")
assert ".gmail-worker-2.pid" in command
assert ".gmail-worker-2.env" in command
assert "kill -0" in command
assert "then kill" in command
assert "pgrep" not in command
assert '. "$environment_file"' in command
assert "nohup .venv/bin/python" in command
assert "VERIGO_REMOTE_WORKER_RELEASE=release-123" in command
assert "verigo-gmail-worker-2.log" in command
assert "if ! test -x .venv/bin/python" in command
assert ".verigo-worker-deps-sha256" in command
assert command.count(".venv/bin/pip -q install") == 1
assert "python3 -m venv .venv && .venv/bin/pip" not in command
try:
    CloudShellLifecycle._worker_command(0)
except ValueError as exc:
    assert str(exc) == "Cloud Shell worker number must be positive"
else:
    raise AssertionError("Cloud Shell worker number must be positive")
assert CloudShellLifecycle._cloudshell_public_key("ssh-rsa payload comment") == "ssh-rsa payload"
try:
    CloudShellLifecycle._cloudshell_public_key("ssh-ed25519 payload")
except RuntimeError as exc:
    assert str(exc) == "Cloud Shell public key must use RSA or ECDSA"
else:
    raise AssertionError("Cloud Shell must reject unsupported Ed25519 API keys")

with tempfile.TemporaryDirectory() as directory:
    manifest = Path(directory) / "accounts.json"
    manifest.write_text(json.dumps({"accounts": [{
        "id": "account3",
        "user": "user-3@example.invalid",
        "quota_project": "project-3",
        "adc_path": "/tmp/account3-adc.json",
        "ssh_key_path": "/tmp/account3-ed25519",
        "worker_processes": 3,
    }, {
        "id": "invalid",
        "user": "",
        "quota_project": "project-invalid",
        "adc_path": "/tmp/invalid",
        "ssh_key_path": "/tmp/invalid",
    }]}), encoding="utf-8")
    specs = _load_cloudshell_account_specs(manifest)
    assert len(specs) == 1
    assert specs[0]["worker_id"] == "cloudshell-gmail-account3"
    assert specs[0]["worker_processes"] == 3
lifecycle = CloudShellLifecycle()
lifecycle.start()
assert lifecycle._thread is not None and lifecycle._thread.is_alive()
lifecycle.stop()
assert lifecycle._thread is None

bootstrap = CloudShellLifecycle(worker_processes=2, worker_id="cloudshell-test")
bootstrap_calls: list[tuple[list[str], bytes | None]] = []
assert bootstrap._lock.acquire(blocking=False)


def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
    payload = kwargs.get("input")
    bootstrap_calls.append((command, payload if isinstance(payload, bytes) else None))
    if command[0] == "tar":
        return SimpleNamespace(returncode=0, stdout=b"archive", stderr=b"")
    return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


with patch.object(bootstrap, "_token", return_value="token"), patch.object(
    bootstrap, "_public_key", return_value="ssh-rsa key"
), patch.object(
    bootstrap,
    "_start_environment",
    return_value={"state": "RUNNING", "sshHost": "host", "sshPort": 2222, "sshUsername": "user"},
), patch(
    "app.core.cloudshell_lifecycle.subprocess.run", side_effect=fake_run
), patch("app.core.cloudshell_lifecycle.Path.read_text", return_value="release-123\n"):
    bootstrap._start()

uploaded_environments = [
    payload.decode()
    for command, payload in bootstrap_calls
    if payload and any("cat > ~/verigo-worker/.gmail-worker-" in part for part in command)
]
assert len(uploaded_environments) == 2
assert "VERIGO_TENCENT_QQ_WORKER_ID=cloudshell-test-1" in uploaded_environments[0]
assert "VERIGO_TENCENT_QQ_WORKER_ID=cloudshell-test-2" in uploaded_environments[1]
assert all("VERIGO_REMOTE_WORKER_CAPACITY=1" in value for value in uploaded_environments)
assert any(".gmail-worker-1.pid" in part for command, _ in bootstrap_calls for part in command)
assert any(".gmail-worker-2.pid" in part for command, _ in bootstrap_calls for part in command)

timeout_bootstrap = CloudShellLifecycle(worker_id="cloudshell-timeout")
assert timeout_bootstrap._lock.acquire(blocking=False)
probe_calls = [0]


def timeout_then_succeed(command: list[str], **kwargs: object) -> SimpleNamespace:
    if command[0] == "tar":
        return SimpleNamespace(returncode=0, stdout=b"archive", stderr=b"")
    if command[-1] == "true":
        probe_calls[0] += 1
        if probe_calls[0] == 1:
            raise subprocess.TimeoutExpired(command, 15)
    return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


with patch.object(timeout_bootstrap, "_token", return_value="token"), patch.object(
    timeout_bootstrap, "_public_key", return_value="ssh-rsa key"
), patch.object(
    timeout_bootstrap,
    "_start_environment",
    return_value={"state": "RUNNING", "sshHost": "host", "sshPort": 2222, "sshUsername": "user"},
), patch(
    "app.core.cloudshell_lifecycle.subprocess.run", side_effect=timeout_then_succeed
), patch("app.core.cloudshell_lifecycle.Path.read_text", return_value="release-123\n"), patch(
    "app.core.cloudshell_lifecycle.time.sleep"
):
    timeout_bootstrap._start()
assert probe_calls[0] == 2
assert not timeout_bootstrap._lock.locked()

environment = CloudShellLifecycle._operation_environment({
    "done": True,
    "response": {"environment": {"sshHost": "host", "sshPort": 2222}},
})
assert environment == {"sshHost": "host", "sshPort": 2222}
assert CloudShellLifecycle._operation_environment({"done": False, "name": "operations/1"}) is None
try:
    CloudShellLifecycle._operation_environment({"error": {"message": "denied"}})
except RuntimeError as exc:
    assert str(exc) == "denied"
else:
    raise AssertionError("Cloud Shell operation errors must be surfaced")

print("cloudshell lifecycle smoke: ok")
