from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.cloudshell_lifecycle import CloudShellLifecycle


command = CloudShellLifecycle._worker_command()
assert ".gmail-worker.pid" in command
assert "kill -0" in command
assert "pgrep" not in command
assert ". .worker.env" in command
assert "nohup .venv/bin/python" in command
assert 'VERIGO_REMOTE_WORKER_TARGET=gmail' in command
lifecycle = CloudShellLifecycle()
lifecycle.start()
assert lifecycle._thread is not None and lifecycle._thread.is_alive()
lifecycle.stop()
assert lifecycle._thread is None

print("cloudshell lifecycle smoke: ok")
