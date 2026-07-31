"""Run remote verification-node supervision outside the HTTP service."""
from __future__ import annotations

import signal
import threading
import time

from app.core.cloudshell_lifecycle import GMAIL_TARGET, start_cloudshell_lifecycles, stop_cloudshell_lifecycles
from app.core.worker_lifecycle import DOMESTIC_CLOUDSTUDIO_TARGET, worker_lifecycle
from app.config import settings
from app.db.jobs import job_store


stop_event = threading.Event()


def main() -> None:
    job_store.initialize()
    worker_lifecycle.start()
    start_cloudshell_lifecycles()
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    next_node_reconcile = 0.0
    while not stop_event.wait(1):
        if time.monotonic() >= next_node_reconcile:
            job_store.reconcile_worker_nodes()
            for target, label in (
                (GMAIL_TARGET, "Cloud Shell"),
                (DOMESTIC_CLOUDSTUDIO_TARGET, "Cloud Studio"),
            ):
                job_store.reroute_stale_queued_jobs(
                    target,
                    "local",
                    settings.remote_worker_fallback_seconds,
                    f"{label} worker did not claim this task in time; reassigned to local verification",
                )
            # CodeArts is a peer worker, not a fallback chain. Its experience
            # workspace can disappear without notifying Verigo, so release
            # only unclaimed CodeArts jobs after the same bounded wait.
            job_store.reroute_stale_queued_jobs(
                "codearts",
                "local",
                settings.remote_worker_fallback_seconds,
                "CodeArts worker did not claim this task in time; reassigned to local verification",
            )
            next_node_reconcile = time.monotonic() + 30
    stop_cloudshell_lifecycles()
    worker_lifecycle.stop()


if __name__ == "__main__":
    main()
