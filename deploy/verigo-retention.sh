#!/usr/bin/env bash
set -Eeuo pipefail

cd /opt/verigo
config=/etc/verigo/retention.env
if [[ -r "$config" ]]; then
    # shellcheck disable=SC1090
    source "$config"
fi
results_days=${VERIGO_RESULTS_RETENTION_DAYS:-30}
job_days=${VERIGO_JOB_RETENTION_DAYS:-90}
if ! [[ "$results_days" =~ ^[1-9][0-9]*$ && "$job_days" =~ ^[1-9][0-9]*$ ]]; then
    echo "Retention periods must be positive integers" >&2
    exit 1
fi

RESULTS_DAYS="$results_days" JOB_DAYS="$job_days" /opt/verigo/.venv/bin/python - <<'PY'
import os
import sqlite3
from datetime import timedelta
from pathlib import Path

from app.db.jobs import utc_now

database = Path('/opt/verigo/data/verigo.db')
results_root = Path('/opt/verigo/data/results').resolve()
now = utc_now()
results_cutoff = (now - timedelta(days=int(os.environ['RESULTS_DAYS']))).isoformat()
jobs_cutoff = (now - timedelta(days=int(os.environ['JOB_DAYS']))).isoformat()

with sqlite3.connect(database) as connection:
    connection.execute('PRAGMA foreign_keys=ON')
    stale_files = connection.execute(
        """SELECT id, csv_path FROM jobs
        WHERE status IN ('completed', 'failed') AND finished_at < ?""",
        (results_cutoff,),
    ).fetchall()
    for job_id, csv_path in stale_files:
        if csv_path:
            candidate = Path(csv_path).resolve()
            if candidate.is_relative_to(results_root):
                candidate.unlink(missing_ok=True)
        connection.execute(
            "UPDATE jobs SET results_json='[]', csv_path=NULL WHERE id=?", (job_id,)
        )

    expired_jobs = connection.execute(
        """SELECT id, csv_path FROM jobs
        WHERE status IN ('completed', 'failed') AND finished_at < ?""",
        (jobs_cutoff,),
    ).fetchall()
    for job_id, csv_path in expired_jobs:
        if csv_path:
            candidate = Path(csv_path).resolve()
            if candidate.is_relative_to(results_root):
                candidate.unlink(missing_ok=True)
        connection.execute('DELETE FROM catch_all_emails WHERE job_id=?', (job_id,))
        connection.execute('DELETE FROM jobs WHERE id=?', (job_id,))
    connection.execute('DELETE FROM verification_cache WHERE expires_at <= ?', (now.isoformat(),))
PY
