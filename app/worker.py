from __future__ import annotations

import argparse
import logging
import signal
import socket
import threading
import time

from app.config import settings
from app.db.jobs import Job, job_store
from app.tasks.verification import run_job


logger = logging.getLogger(__name__)
stop_event = threading.Event()


def _heartbeat_loop(job: Job, done: threading.Event) -> None:
    interval = max(5.0, min(30.0, settings.worker_lease_seconds / 3))
    while not done.wait(interval):
        if job.lease_id:
            job_store.heartbeat_lease(job.id, job.worker_id or "", job.lease_id)
        else:
            job_store.heartbeat(job)


def run(worker_name: str) -> None:
    job_store.initialize()
    logger.info("Verification worker %s is ready", worker_name)
    while not stop_event.is_set():
        job = job_store.claim_remote_lease(
            worker_name,
            "local",
            shard_size=settings.scheduler_remote_shard_size,
        )
        if job is None:
            # Discovery needs deterministic, stop-after-match ordering and is
            # intentionally excluded from the shared work-stealing path.
            job = job_store.claim_next(
                worker_name,
                stop_on_deliverable_only=True,
            )
        if job is None:
            stop_event.wait(settings.worker_poll_seconds)
            continue
        done = threading.Event()
        heartbeat = threading.Thread(target=_heartbeat_loop, args=(job, done), daemon=True)
        heartbeat.start()
        try:
            run_job(job)
        finally:
            done.set()
            heartbeat.join(timeout=2)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default=f"{socket.gethostname()}-{threading.get_native_id()}")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    run(args.name)


if __name__ == "__main__":
    main()
