"""Regression checks for immutable first-completion timestamps."""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path


temp_dir = Path(tempfile.mkdtemp(prefix="verigo-initial-completion-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["VERIGO_DATABASE_URL"] = ""
os.environ["VERIGO_DATABASE_PATH"] = str(temp_dir / "verigo.db")
os.environ["VERIGO_RESULTS_DIR"] = str(temp_dir / "results")

from app.db.jobs import Job, JobStore  # noqa: E402
from app.db.sqlite import connect as connect_sqlite  # noqa: E402


def main() -> int:
    store = JobStore()
    store._connect = lambda: connect_sqlite(temp_dir / "verigo.db")  # type: ignore[method-assign]
    store.initialize()
    job = Job(id="immutable-completion", emails=["first@example.com"], worker_count=1)
    store.add(job)

    with store._connect() as connection:
        pending = connection.execute(
            "SELECT initial_completed_at FROM job_results WHERE job_id=?",
            (job.id,),
        ).fetchone()[0]
    assert pending is None

    store.upsert_results(job.id, [{
        "email": "first@example.com",
        "original_index": 0,
        "deliverable": None,
        "progress_state": "completed",
    }])
    with store._connect() as connection:
        initial, first_update = connection.execute(
            "SELECT initial_completed_at, updated_at FROM job_results WHERE job_id=?",
            (job.id,),
        ).fetchone()
    assert initial == first_update

    time.sleep(0.01)
    store.upsert_results(job.id, [{
        "email": "first@example.com",
        "original_index": 0,
        "deliverable": True,
        "progress_state": "completed",
        "retry_updated": True,
        "retry_state": "completed",
    }])
    with store._connect() as connection:
        reviewed_initial, reviewed_update = connection.execute(
            "SELECT initial_completed_at, updated_at FROM job_results WHERE job_id=?",
            (job.id,),
        ).fetchone()
    assert reviewed_initial == initial
    assert reviewed_update > first_update
    print("initial completion smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
