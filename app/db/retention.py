"""Backend-aware job/result retention (P0.6)."""
from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from typing import Iterable

from app.config import settings
from app.db.backend_ops import database_url, postgres_enabled, sqlite_path
from app.db.jobs import utc_now
from app.db.sqlite import begin_immediate, connect as sqlite_connect

BATCH_SIZE = 200


def chunks(values: list[str], size: int = BATCH_SIZE) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def placeholders(values: list[str], *, style: str = "sqlite") -> str:
    if style == "postgres":
        return ", ".join(["%s"] * len(values))
    return ", ".join("?" for _ in values)


def delete_csv_paths(rows: list[tuple[str, str | None]], results_root: Path) -> None:
    for _job_id, csv_path in rows:
        if not csv_path:
            continue
        candidate = Path(csv_path).resolve()
        if candidate.is_relative_to(results_root):
            candidate.unlink(missing_ok=True)


def run_retention(
    *,
    results_days: int | None = None,
    job_days: int | None = None,
    analytics_unique_days: int | None = None,
    results_root: Path | None = None,
) -> dict[str, int]:
    results_days = int(results_days or os.getenv("VERIGO_RESULTS_RETENTION_DAYS", "30"))
    job_days = int(job_days or os.getenv("VERIGO_JOB_RETENTION_DAYS", "90"))
    analytics_unique_days = int(
        analytics_unique_days
        or os.getenv("VERIGO_ANALYTICS_UNIQUE_RETENTION_DAYS", "45")
    )
    results_root = (results_root or Path(settings.results_dir)).resolve()
    now = utc_now()
    results_cutoff = now - timedelta(days=results_days)
    jobs_cutoff = now - timedelta(days=job_days)
    analytics_cutoff_day = (now - timedelta(days=analytics_unique_days)).date().isoformat()

    if postgres_enabled():
        return _run_postgres(
            results_cutoff, jobs_cutoff, analytics_cutoff_day, results_root, now,
        )
    return _run_sqlite(
        results_cutoff, jobs_cutoff, analytics_cutoff_day, results_root, now,
    )


def _run_sqlite(
    results_cutoff, jobs_cutoff, analytics_cutoff_day: str, results_root: Path, now,
) -> dict[str, int]:
    database = sqlite_path()
    results_iso = results_cutoff.isoformat()
    jobs_iso = jobs_cutoff.isoformat()
    with sqlite_connect(database) as connection:
        stale_rows = connection.execute(
            """SELECT id, csv_path FROM jobs
            WHERE status IN ('completed', 'failed') AND finished_at < ?""",
            (results_iso,),
        ).fetchall()
        expired_rows = connection.execute(
            """SELECT id, csv_path FROM jobs
            WHERE status IN ('completed', 'failed') AND finished_at < ?""",
            (jobs_iso,),
        ).fetchall()

    stale_by_id = {str(job_id): csv_path for job_id, csv_path in stale_rows}
    expired_by_id = {str(job_id): csv_path for job_id, csv_path in expired_rows}
    delete_csv_paths(list({**stale_by_id, **expired_by_id}.items()), results_root)

    cleared = 0
    deleted = 0
    with sqlite_connect(database) as connection:
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
            cleared += len(job_ids)

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
            deleted += len(job_ids)

        begin_immediate(connection)
        connection.execute(
            "DELETE FROM verification_cache WHERE expires_at <= ?",
            (now.isoformat(),),
        )
        metrics_table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='company_finder_event_users'"
        ).fetchone()
        analytics_users_deleted = 0
        if metrics_table_exists:
            analytics_users_deleted = connection.execute(
                "DELETE FROM company_finder_event_users WHERE day < ?",
                (analytics_cutoff_day,),
            ).rowcount
        connection.commit()

    return {
        "backend": "sqlite", "results_cleared_jobs": cleared, "jobs_deleted": deleted,
        "analytics_users_deleted": max(0, analytics_users_deleted),
    }  # type: ignore[return-value]


def _run_postgres(
    results_cutoff, jobs_cutoff, analytics_cutoff_day: str, results_root: Path, now,
) -> dict[str, int]:
    from app.db.postgresql import connection as pg_connection, resolve_database_url

    dsn = resolve_database_url(database_url() or None)
    with pg_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, csv_path FROM jobs
                WHERE status IN ('completed', 'failed') AND finished_at < %s
                """,
                (results_cutoff,),
            )
            stale_rows = [(str(r["id"]), r["csv_path"]) for r in cur.fetchall()]
            cur.execute(
                """
                SELECT id, csv_path FROM jobs
                WHERE status IN ('completed', 'failed') AND finished_at < %s
                """,
                (jobs_cutoff,),
            )
            expired_rows = [(str(r["id"]), r["csv_path"]) for r in cur.fetchall()]

    stale_by_id = {job_id: csv_path for job_id, csv_path in stale_rows}
    expired_by_id = {job_id: csv_path for job_id, csv_path in expired_rows}
    delete_csv_paths(list({**stale_by_id, **expired_by_id}.items()), results_root)

    cleared = 0
    deleted = 0
    with pg_connection(dsn) as conn:
        with conn.cursor() as cur:
            for job_ids in chunks([job_id for job_id in stale_by_id if job_id not in expired_by_id]):
                marks = placeholders(job_ids, style="postgres")
                cur.execute(
                    f"UPDATE jobs SET results_json='[]', csv_path=NULL WHERE id IN ({marks})",
                    job_ids,
                )
                cur.execute(f"DELETE FROM job_results WHERE job_id IN ({marks})", job_ids)
                cur.execute(
                    f"DELETE FROM job_result_links WHERE child_job_id IN ({marks}) "
                    f"OR parent_job_id IN ({marks})",
                    (*job_ids, *job_ids),
                )
                cur.execute(f"DELETE FROM catch_all_emails WHERE job_id IN ({marks})", job_ids)
                cleared += len(job_ids)

            for job_ids in chunks(list(expired_by_id)):
                marks = placeholders(job_ids, style="postgres")
                cur.execute(f"SELECT id FROM job_leases WHERE job_id IN ({marks})", job_ids)
                lease_ids = [str(row["id"]) for row in cur.fetchall()]
                if lease_ids:
                    lease_marks = placeholders(lease_ids, style="postgres")
                    cur.execute(
                        f"DELETE FROM mx_scheduler_leases WHERE lease_id IN ({lease_marks})",
                        lease_ids,
                    )
                cur.execute(f"DELETE FROM catch_all_emails WHERE job_id IN ({marks})", job_ids)
                cur.execute(f"DELETE FROM job_results WHERE job_id IN ({marks})", job_ids)
                cur.execute(
                    f"DELETE FROM job_result_links WHERE child_job_id IN ({marks}) "
                    f"OR parent_job_id IN ({marks})",
                    (*job_ids, *job_ids),
                )
                cur.execute(f"DELETE FROM job_leases WHERE job_id IN ({marks})", job_ids)
                cur.execute(f"DELETE FROM jobs WHERE id IN ({marks})", job_ids)
                deleted += len(job_ids)

            cur.execute(
                "DELETE FROM verification_cache WHERE expires_at <= %s",
                (now,),
            )
            cur.execute(
                "DELETE FROM company_finder_event_users WHERE day < %s",
                (analytics_cutoff_day,),
            )
            analytics_users_deleted = max(0, cur.rowcount)

    return {
        "backend": "postgres", "results_cleared_jobs": cleared, "jobs_deleted": deleted,
        "analytics_users_deleted": analytics_users_deleted,
    }  # type: ignore[return-value]


if __name__ == "__main__":
    import json

    print(json.dumps(run_retention(), default=str))
