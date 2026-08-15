"""Run remote verification-node supervision outside the HTTP service."""
from __future__ import annotations

import signal
import threading
import time
import logging

from app.core.cloudshell_lifecycle import GMAIL_TARGET, start_cloudshell_lifecycles, stop_cloudshell_lifecycles
from app.core.worker_lifecycle import DOMESTIC_CLOUDSTUDIO_TARGET, worker_lifecycle
from app.config import settings
from app.db.jobs import job_store
from app.tasks.verification import reconcile_orphaned_background_retries


stop_event = threading.Event()
logger = logging.getLogger(__name__)


def main() -> None:
    job_store.initialize()
    worker_lifecycle.start()
    start_cloudshell_lifecycles()
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    next_node_reconcile = 0.0
    next_retry_reconcile = 0.0
    while not stop_event.wait(1):
        if time.monotonic() >= next_node_reconcile:
            # A concurrent API transaction can briefly hold a PostgreSQL row
            # lock. Keep every lifecycle loop alive and retry maintenance on
            # the next pass rather than terminating the supervisor.
            try:
                job_store.reconcile_worker_nodes()
                for target, label in (
                    (DOMESTIC_CLOUDSTUDIO_TARGET, "Cloud Studio"),
                    ("codearts", "CodeArts"),
                ):
                    job_store.reroute_stale_queued_jobs(
                        target,
                        "local",
                        settings.remote_worker_fallback_seconds,
                        f"{label} worker did not claim this task in time; reassigned to local verification",
                    )
            except Exception:  # noqa: BLE001
                logger.exception("Remote worker maintenance pass failed; retrying")
            next_node_reconcile = time.monotonic() + 30
        if time.monotonic() >= next_retry_reconcile:
            try:
                summary = reconcile_orphaned_background_retries(parent_limit=25)
                if summary["results"]:
                    logger.info("Settled orphaned verification reviews: %s", summary)
            except Exception:  # noqa: BLE001
                logger.exception("Orphaned review reconciliation failed; retrying")
            next_retry_reconcile = time.monotonic() + 300
    stop_cloudshell_lifecycles()
    worker_lifecycle.stop()


if __name__ == "__main__":
    main()
