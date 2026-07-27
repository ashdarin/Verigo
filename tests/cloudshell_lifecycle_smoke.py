from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.cloudshell_lifecycle import CloudShellLifecycle


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
    bootstrap, "_start_environment", return_value={"sshHost": "host", "sshPort": 2222}
), patch.object(bootstrap, "_add_public_key"), patch(
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
