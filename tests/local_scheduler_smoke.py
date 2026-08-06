from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


temp_dir = Path(tempfile.mkdtemp(prefix="verigo-local-scheduler-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
os.environ["VERIGO_DATABASE_PATH"] = str(temp_dir / "verigo.db")
os.environ["VERIGO_RESULTS_DIR"] = str(temp_dir / "results")

from app.db.jobs import Job, job_store
import app.tasks.verification as verification


class FakeVerifier:
    def verify_batch_distributed(self, emails, num_processes, result_callback, should_stop):
        assert num_processes == 4
        results = []
        for index, email in enumerate(emails):
            result = {
                "email": email,
                "original_index": index,
                "valid": True,
                "deliverable": True,
                "checks": {"smtp": True},
                "mx_records": ["mx.unit.test."],
                "smtp_result": "250 accepted",
                "progress_state": "completed",
            }
            result_callback(result)
            results.append(result)
        return results


class StopDuringVerifier(FakeVerifier):
    def verify_batch_distributed(self, emails, num_processes, result_callback, should_stop):
        result = {
            "email": emails[0],
            "original_index": 0,
            "valid": True,
            "deliverable": True,
            "checks": {"smtp": True},
            "progress_state": "completed",
        }
        result_callback(result)
        job_store.stop("stop-during-run")
        return [result]


original_create_verifier = verification.create_verifier
verification.create_verifier = lambda _workers: FakeVerifier()
try:
    job = Job(
        id="local-scheduled",
        emails=["first@unit.test", "second@unit.test"],
        worker_count=4,
        execution_target="local",
    )
    job_store.add(job)

    first = job_store.claim_remote_lease("local-a", "local", shard_size=1)
    assert first is not None and first.pending_indices == [0]
    verification.run_job(first)
    after_first = job_store.get(job.id)
    assert after_first is not None and after_first.status == "running"
    assert after_first.results[0]["deliverable"] is True
    assert after_first.results[1]["progress_state"] == "pending"

    second = job_store.claim_remote_lease("local-b", "local", shard_size=1)
    assert second is not None and second.pending_indices == [1]
    verification.run_job(second)
    completed = job_store.get(job.id)
    assert completed is not None and completed.status == "completed"
    assert all(result["deliverable"] is True for result in completed.results)

    discovery = Job(
        id="local-discovery",
        emails=["person@ordered.test"],
        worker_count=1,
        execution_target="local",
        stop_on_deliverable=True,
    )
    job_store.add(discovery)
    assert job_store.claim_remote_lease("local-c", "local", shard_size=1) is None
    ordered = job_store.claim_next("local-c", stop_on_deliverable_only=True)
    assert ordered is not None and ordered.id == discovery.id

    # A worker finishing from an old in-memory snapshot must not resurrect a
    # job that the user stopped while verification was still in progress.
    stopped = Job(id="stop-during-run", emails=["stop@unit.test"], worker_count=1)
    job_store.add(stopped)
    claimed = job_store.claim_next("stale-worker")
    assert claimed is not None and claimed.id == stopped.id
    stop_verifier_factory = verification.create_verifier
    verification.create_verifier = lambda _workers: StopDuringVerifier()
    verification.run_job(claimed)
    verification.create_verifier = stop_verifier_factory
    after_stop = job_store.get(stopped.id)
    assert after_stop is not None and after_stop.status == "stopped"
    assert after_stop.worker_id is None
finally:
    verification.create_verifier = original_create_verifier

print("local scheduler smoke: ok")
