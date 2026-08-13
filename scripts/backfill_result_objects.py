#!/usr/bin/env python3
"""Backfill result_objects from completed, owned, user-visible jobs.

Finds jobs with status='completed', owner_id set, and parent_id IS NULL, then
calls ResultObjectStore.publish_completed_job for settled job_results that are
not already stored. Safe to run twice (existing owner/task/index rows are
skipped).

Connects via VERIGO_DATABASE_URL, or loads it from postgres.env. The DSN is
never printed.

Usage (on a host that already has postgres.env / VERIGO_DATABASE_URL):

  PYTHONPATH=. python scripts/backfill_result_objects.py --limit 100
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_DB_KEYS = (
    "VERIGO_DATABASE_URL",
    "POSTGRES_DSN",
    "DATABASE_URL",
    "VERIGO_POSTGRES_ENABLED",
)
_PENDING = frozenset({"pending", "verifying"})
_DSN_URL_RE = re.compile(r"postgresql(?:\+\w+)?://\S+", re.I)
_DSN_PASSWORD_RE = re.compile(r":[^:@/\s]+@")


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    text = path.read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def configure_database_env(*, env_file: Path | None = None) -> None:
    """Populate VERIGO_DATABASE_URL from the environment or postgres.env.

    Existing process env wins. File contents and the DSN are never printed.
    """
    if os.environ.get("VERIGO_DATABASE_URL", "").strip():
        return

    candidates: list[Path] = []
    if env_file is not None:
        candidates.append(env_file)
    explicit = os.environ.get("VERIGO_POSTGRES_ENV", "").strip()
    if explicit:
        candidates.append(Path(explicit))
    candidates.extend((Path("/etc/verigo/postgres.env"), ROOT / "postgres.env"))

    for path in candidates:
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        parsed = _parse_env_file(path)
        for key in _DB_KEYS:
            value = (parsed.get(key) or "").strip()
            if value and not (os.environ.get(key) or "").strip():
                os.environ[key] = value
        if os.environ.get("VERIGO_DATABASE_URL", "").strip():
            break

    if not os.environ.get("VERIGO_DATABASE_URL", "").strip():
        for key in ("POSTGRES_DSN", "DATABASE_URL"):
            value = (os.environ.get(key) or "").strip()
            if value:
                os.environ["VERIGO_DATABASE_URL"] = value
                break

    if os.environ.get("VERIGO_DATABASE_URL", "").strip():
        os.environ.setdefault("VERIGO_POSTGRES_ENABLED", "true")


def _safe_error(exc: BaseException) -> str:
    text = _DSN_URL_RE.sub("postgresql://***", str(exc))
    text = _DSN_PASSWORD_RE.sub(":***@", text)
    return f"{type(exc).__name__}: {text[:240]}"


def _job_rows(connection: Any, limit: int) -> list[Any]:
    return connection.execute(
        """
        SELECT id, owner_id, parent_id, retry_parent_id,
               execution_target, stop_on_deliverable, emails_json
        FROM jobs
        WHERE status = 'completed'
          AND owner_id IS NOT NULL
          AND owner_id <> ''
          AND parent_id IS NULL
        ORDER BY created_at ASC, id ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def _settled_results(connection: Any, job_id: str, as_json) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT original_index, progress_state, result_json
        FROM job_results
        WHERE job_id = ?
        ORDER BY original_index
        """,
        (job_id,),
    ).fetchall()
    settled: list[dict[str, Any]] = []
    for original_index, progress_state, raw in rows:
        state = str(progress_state or "")
        if state in _PENDING:
            continue
        result = dict(as_json(raw, default={}) or {})
        result["progress_state"] = state
        result["original_index"] = int(result.get("original_index", original_index))
        settled.append(result)
    return settled


def _existing_indexes(connection: Any, owner_id: str, task_id: str) -> set[int]:
    rows = connection.execute(
        "SELECT result_index FROM result_objects WHERE owner_id = ? AND task_id = ?",
        (owner_id, task_id),
    ).fetchall()
    return {int(row[0]) for row in rows}


def backfill(*, limit: int) -> dict[str, int]:
    """Publish missing result_objects for up to ``limit`` completed parent jobs."""
    from app.db.pg_compat import as_bool, as_json, connect_app
    from app.db.result_objects import ResultObjectStore

    store = ResultObjectStore()
    stats = {
        "jobs_scanned": 0,
        "rows_inserted": 0,
        "rows_skipped": 0,
        "errors": 0,
    }

    with closing(connect_app()) as connection:
        jobs = _job_rows(connection, limit)
        for row in jobs:
            stats["jobs_scanned"] += 1
            job_id = str(row["id"])
            owner_id = str(row["owner_id"] or "")
            parent_id = row["parent_id"]
            retry_parent_id = row["retry_parent_id"]
            execution_target = str(row["execution_target"] or "local")
            stop_on_deliverable = as_bool(row["stop_on_deliverable"])
            emails = list(as_json(row["emails_json"], default=[]) or [])
            if not owner_id or parent_id or retry_parent_id:
                continue
            try:
                settled = _settled_results(connection, job_id, as_json)
                existing = _existing_indexes(connection, owner_id, job_id)
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                print(f"error job={job_id} {_safe_error(exc)}", file=sys.stderr)
                continue

            missing: list[dict[str, Any]] = []
            for result in settled:
                index = int(result["original_index"])
                if index in existing:
                    stats["rows_skipped"] += 1
                else:
                    missing.append(result)

            if not missing:
                continue

            job = SimpleNamespace(
                id=job_id,
                owner_id=owner_id,
                parent_id=None,
                retry_parent_id=None,
                execution_target=execution_target,
                stop_on_deliverable=stop_on_deliverable,
                emails=emails,
                results=missing,
            )
            try:
                written = int(store.publish_completed_job(job) or 0)
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += len(missing)
                print(f"error job={job_id} {_safe_error(exc)}", file=sys.stderr)
                continue
            if written < 0:
                written = 0
            if written > len(missing):
                written = len(missing)
            stats["rows_inserted"] += written
            stats["errors"] += len(missing) - written
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        metavar="N",
        help="Max completed parent jobs to scan (default: 100).",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Optional postgres.env path. Ignored when VERIGO_DATABASE_URL is set.",
    )
    args = parser.parse_args()
    if args.limit < 1:
        print("error: --limit must be >= 1", file=sys.stderr)
        return 2

    configure_database_env(env_file=args.env_file)
    if not os.environ.get("VERIGO_DATABASE_URL", "").strip():
        print(
            "PostgreSQL DSN not configured. Set VERIGO_DATABASE_URL or provide postgres.env.",
            file=sys.stderr,
        )
        return 2

    try:
        stats = backfill(limit=args.limit)
    except Exception as exc:  # noqa: BLE001
        print(_safe_error(exc), file=sys.stderr)
        return 1

    print(f"jobs_scanned={stats['jobs_scanned']}")
    print(f"rows_inserted={stats['rows_inserted']}")
    print(f"rows_skipped={stats['rows_skipped']}")
    print(f"errors={stats['errors']}")
    return 1 if stats["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
