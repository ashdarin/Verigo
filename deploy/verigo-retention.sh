#!/usr/bin/env bash
set -Eeuo pipefail

cd /opt/verigo/current
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
from datetime import timedelta
from pathlib import Path
from typing import Iterable

from app.db.sqlite import begin_immediate, connect
from app.db.jobs import utc_now

database = Path('/opt/verigo/data/verigo.db')
results_root = Path('/opt/verigo/data/results').resolve()
now = utc_now()
results_cutoff = (now - timedelta(days=int(os.environ['RESULTS_DAYS']))).isoformat()
jobs_cutoff = (now - timedelta(days=int(os.environ['JOB_DAYS']))).isoformat()
BATCH_SIZE = 200


def chunks(values: list[str], size: int = BATCH_SIZE) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def delete_csv_paths(rows: list[tuple[str, str | None]]) -> None:
    for _job_id, csv_path in rows:
        if not csv_path:
            continue
        candidate = Path(csv_path).resolve()
        if candidate.is_relative_to(results_root):
            candidate.unlink(missing_ok=True)


def placeholders(values: list[str]) -> str:
    return ", ".join("?" for _ in values)


with connect(database) as connection:
    stale_rows = connection.execute(
        """SELECT id, csv_path FROM jobs
        WHERE status IN ('completed', 'failed') AND finished_at < ?""",
        (results_cutoff,),
    ).fetchall()
    expired_rows = connection.execute(
        """SELECT id, csv_path FROM jobs
        WHERE status IN ('completed', 'failed') AND finished_at < ?""",
        (jobs_cutoff,),
    ).fetchall()

stale_by_id = {str(job_id): csv_path for job_id, csv_path in stale_rows}
expired_by_id = {str(job_id): csv_path for job_id, csv_path in expired_rows}
delete_csv_paths(list({**stale_by_id, **expired_by_id}.items()))

# File deletion is deliberately outside write transactions. Each database batch
# is small enough that active result callbacks do not wait on retention work.
with connect(database) as connection:
    for job_ids in chunks([job_id for job_id in stale_by_id if job_id not in expired_by_id]):
        marks = placeholders(job_ids)
        begin_immediate(connection)
        connection.execute(
            f"UPDATE jobs SET results_json='[]', csv_path=NULL WHERE id IN ({marks})",
            job_ids,
        )
        connection.execute(f"DELETE FROM job_results WHERE job_id IN ({marks})", job_ids)
        connection.execute(
            f"DELETE FROM job_result_links WHERE child_job_id IN ({marks}) "
            f"OR parent_job_id IN ({marks})",
            (*job_ids, *job_ids),
        )
        connection.execute(f"DELETE FROM catch_all_emails WHERE job_id IN ({marks})", job_ids)
        connection.commit()

    for job_ids in chunks(list(expired_by_id)):
        marks = placeholders(job_ids)
        begin_immediate(connection)
        lease_ids = [
            str(row[0])
            for row in connection.execute(
                f"SELECT id FROM job_leases WHERE job_id IN ({marks})", job_ids
            ).fetchall()
        ]
        if lease_ids:
            lease_marks = placeholders(lease_ids)
            connection.execute(
                f"DELETE FROM mx_scheduler_leases WHERE lease_id IN ({lease_marks})",
                lease_ids,
            )
        connection.execute(f"DELETE FROM catch_all_emails WHERE job_id IN ({marks})", job_ids)
        connection.execute(f"DELETE FROM job_results WHERE job_id IN ({marks})", job_ids)
        connection.execute(
            f"DELETE FROM job_result_links WHERE child_job_id IN ({marks}) "
            f"OR parent_job_id IN ({marks})",
            (*job_ids, *job_ids),
        )
        connection.execute(f"DELETE FROM job_leases WHERE job_id IN ({marks})", job_ids)
        connection.execute(f"DELETE FROM jobs WHERE id IN ({marks})", job_ids)
        connection.commit()

    begin_immediate(connection)
    connection.execute('DELETE FROM verification_cache WHERE expires_at <= ?', (now.isoformat(),))
    connection.commit()
PY
