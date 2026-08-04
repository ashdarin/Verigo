from __future__ import annotations

import tempfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.cloudshell_coordinator import CloudShellCoordinator


with tempfile.TemporaryDirectory() as directory:
    coordinator = CloudShellCoordinator(Path(directory) / "coordinator.db")
    coordinator.sync_accounts([
        {"account_id": "a", "worker_id": "rotation-a", "enabled": True},
        {"account_id": "b", "worker_id": "rotation-b", "enabled": True},
    ])
    first = coordinator.reserve("rotation-a", 1)
    assert first is not None
    assert coordinator.commit(first, 1)
    assert coordinator.reserve("rotation-a", 1) is None
    second = coordinator.reserve("rotation-b", 1)
    assert second is not None
    coordinator.release(second)
    snapshot = coordinator.snapshot()
    usage = {item["worker_id"]: item["claimed_units"] for item in snapshot}
    assert usage["rotation-b"] == 0
    assert usage["rotation-a"] == 1

print("cloudshell coordinator smoke: ok")
