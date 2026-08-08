"""Ensure dashboard job timing ignores internal child and retry records."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import sys
import tempfile


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.db.auth import auth_store
from app.db.jobs import Job, job_store, utc_now
from app.db.metrics import metrics_store


original_database_path = settings.database_path
with tempfile.TemporaryDirectory() as raw_dir:
    object.__setattr__(settings, "database_path", Path(raw_dir) / "metrics.db")
    job_store._initialized = False
    auth_store._initialized = False
    metrics_store._initialized = False
    auth_store.initialize()

    now = utc_now()
    visible = Job(
        id="visible-metric",
        emails=["visible@example.com"],
        worker_count=1,
        status="completed",
        created_at=now - timedelta(seconds=3600),
        started_at=now - timedelta(seconds=60),
        finished_at=now,
    )
    visible_failed = Job(
        id="visible-failed",
        emails=["failed@example.com"],
        worker_count=1,
        status="failed",
        created_at=now - timedelta(seconds=120),
        started_at=now - timedelta(seconds=90),
        finished_at=now - timedelta(seconds=30),
    )
    child = Job(
        id="internal-child",
        emails=["child@example.com"],
        worker_count=1,
        status="completed",
        parent_id=visible.id,
        created_at=now - timedelta(seconds=2000),
        started_at=now - timedelta(seconds=1500),
        finished_at=now,
    )
    retry = Job(
        id="internal-retry",
        emails=["retry@example.com"],
        worker_count=1,
        status="completed",
        retry_parent_id=visible.id,
        created_at=now - timedelta(seconds=1800),
        started_at=now - timedelta(seconds=600),
        finished_at=now,
    )
    for job in (visible, visible_failed, child, retry):
        job_store.add(job)

    snapshot = metrics_store.snapshot()
    today = snapshot["today"]
    assert snapshot["totals"]["jobs"] == 2
    assert snapshot["jobs"] == {"queued": 0, "running": 0, "completed": 1, "failed": 1}
    assert today["average_job_seconds"] == 60
    assert today["average_queue_seconds"] == 3540
    assert today["average_retry_wait_seconds"] == 1200

object.__setattr__(settings, "database_path", original_database_path)
print("metrics duration smoke: ok")
