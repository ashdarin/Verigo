from __future__ import annotations

import tempfile
from contextlib import closing
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.cloudshell_coordinator import CloudShellCoordinator
from app.config import settings


# This rotation test needs two eligible accounts. Keep that precondition
# independent from the production idle-pool default.
rotation_min_accounts = settings.cloudshell_active_min_accounts
object.__setattr__(settings, "cloudshell_active_min_accounts", 2)
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
    coordinator.record_failure("rotation-a", "Cloud Shell SSH exited 255")
    coordinator.refresh_pool(force=True)
    snapshot = coordinator.snapshot()
    usage = {item["worker_id"]: item["claimed_units"] for item in snapshot}
    assert usage["rotation-b"] == 0
    assert usage["rotation-a"] == 1
    states = {item["worker_id"]: item["status"] for item in snapshot}
    assert states["rotation-a"] == "cooldown"
    assert states["rotation-b"] == "active"
object.__setattr__(settings, "cloudshell_active_min_accounts", rotation_min_accounts)


with tempfile.TemporaryDirectory() as directory:
    coordinator = CloudShellCoordinator(Path(directory) / "coordinator.db")
    coordinator.sync_accounts([
        {"account_id": f"a{index}", "worker_id": f"worker-{index}", "enabled": True}
        for index in range(1, 5)
    ])
    with closing(coordinator._connect()) as db:
        db.execute("INSERT INTO jobs VALUES ('large-gmail-import', 'gmail', 'queued')")
        db.executemany(
            "INSERT INTO job_results VALUES ('large-gmail-import', 'pending')",
            [()] * 49,
        )
        db.commit()
    original_min = settings.cloudshell_active_min_accounts
    original_max = settings.cloudshell_active_max_accounts
    original_pending = settings.cloudshell_pending_emails_per_active_account
    try:
        object.__setattr__(settings, "cloudshell_active_min_accounts", 1)
        object.__setattr__(settings, "cloudshell_active_max_accounts", 4)
        object.__setattr__(settings, "cloudshell_pending_emails_per_active_account", 48)
        coordinator.refresh_pool(force=True)
        assert sum(item["active"] for item in coordinator.snapshot()) == 2
    finally:
        object.__setattr__(settings, "cloudshell_active_min_accounts", original_min)
        object.__setattr__(settings, "cloudshell_active_max_accounts", original_max)
        object.__setattr__(
            settings, "cloudshell_pending_emails_per_active_account", original_pending
        )

print("cloudshell coordinator smoke: ok")
