from __future__ import annotations

import sqlite3
import sys
import tempfile
from dataclasses import replace
from datetime import timedelta
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings as base_settings
from app.db import jobs as jobs_module
from app.db.jobs import Job, JobStore, utc_now


database_path = Path(tempfile.mkdtemp(prefix="verigo-cross-route-scheduler-")) / "jobs.db"
jobs_module.settings = replace(
    base_settings,
    smtp_cross_route_concurrency=1,
    smtp_cross_route_per_mx_concurrency=1,
)


class SQLiteStore(JobStore):
    def _connect(self):
        return sqlite3.connect(database_path, isolation_level=None)


store = SQLiteStore()
store.initialize()

alternate = Job(
    id="alternate",
    emails=["first@alternate.test", "second@alternate.test"],
    worker_count=8,
    execution_target="local",
    retry_route="alternate_route",
    origin_execution_target="codearts",
    cross_route_attempts=1,
)
normal = Job(
    id="normal",
    emails=["person@normal.test"],
    worker_count=8,
    execution_target="local",
)
store.add(alternate)
store.add(normal)

with store._connect() as connection:
    connection.execute(
        """
        INSERT INTO scheduler_domain_profiles(
            scheduler_key, current_limit, success_streak, successes,
            pressure_events, last_seen_at, cooldown_until
        ) VALUES (?, 1, 0, 0, 1, ?, ?)
        """,
        (
            "domain:alternate.test",
            utc_now().isoformat(),
            (utc_now() + timedelta(minutes=10)).isoformat(),
        ),
    )

# User work stays ahead of a queued diagnostic review.
normal_claim = store.claim_remote_lease("local-normal", "local", shard_size=25)
assert normal_claim is not None and normal_claim.id == "normal"

# The review bypasses the original route's cooldown, but receives one address.
alternate_claim = store.claim_remote_lease("local-alternate", "local", shard_size=25)
assert alternate_claim is not None and alternate_claim.id == "alternate"
assert alternate_claim.pending_indices == [0]

# The global diagnostic budget is one lease even when another worker is idle.
assert store.claim_remote_lease("local-blocked", "local", shard_size=25) is None

assert store.complete_lease_with_results(
    alternate_claim.id,
    alternate_claim.worker_id or "",
    alternate_claim.lease_id or "",
    [{
        "email": "first@alternate.test",
        "original_index": 0,
        "progress_state": "completed",
        "valid": True,
        "deliverable": True,
        "checks": {"smtp": True},
        "smtp_result": "250 accepted",
    }],
)

second_claim = store.claim_remote_lease("local-next", "local", shard_size=25)
assert second_claim is not None and second_claim.id == "alternate"
assert second_claim.pending_indices == [1]

print("smtp cross-route scheduler smoke: ok")
