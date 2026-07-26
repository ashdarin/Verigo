"""Explicit data maintenance commands, kept outside the public web startup."""
from __future__ import annotations

import argparse
import json

from app.db.jobs import job_store


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("migrate-results", "repair"))
    command = parser.parse_args().command
    job_store.initialize()
    if command == "migrate-results":
        print(json.dumps(job_store.migrate_legacy_results(), sort_keys=True))
        return
    print(json.dumps({
        "orphaned_results": job_store.requeue_orphaned_results(),
        "deferred_retries": job_store.release_legacy_deferred_retries(),
        "failed_results": job_store.reconcile_failed_job_results(),
        "parents": job_store.reconcile_aggregate_parents(),
        "retry_notices": job_store.clear_completed_retry_notices(),
        "dns_cache": job_store.clear_dns_negative_cache(),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
