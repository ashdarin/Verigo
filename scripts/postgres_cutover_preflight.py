#!/usr/bin/env python3
"""Final preflight gate for SQLite -> PostgreSQL production cutover."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.postgres_migrate import (  # noqa: E402
    financial_summary_pg,
    financial_summary_sqlite,
    open_sqlite,
    sqlite_tables,
    summarize_pg_table,
    summarize_sqlite_table,
)
from app.db.postgres_schema import TABLES, all_registered_tables, require_registered  # noqa: E402
from app.db.postgresql import (  # noqa: E402
    dsn_uses_local_tunnel,
    resolve_database_url,
    write_rollback_probe,
)
from app.db.postgresql import connection as pg_connection  # noqa: E402


def build_report(
    *,
    sqlite_path: Path,
    dsn: str | None,
    allow_active_leases: bool,
    tables: list[str],
    verify_level: str = "full",
) -> dict[str, Any]:
    blockers: list[str] = []
    warnings: list[str] = []
    differences: dict[str, Any] = {}

    if not sqlite_path.exists():
        blockers.append(f"sqlite_missing:{sqlite_path}")
        return {
            "ready": False,
            "blockers": blockers,
            "warnings": warnings,
            "differences": differences,
        }

    sqlite_conn = open_sqlite(sqlite_path)
    source_table_set = sqlite_tables(sqlite_conn)
    unknown = sorted(source_table_set - set(TABLES))
    if unknown:
        blockers.append("unregistered_source_tables")
        differences["unregistered_source_tables"] = unknown

    try:
        source_active_leases = int(
            sqlite_conn.execute(
                "SELECT COUNT(*) FROM job_leases WHERE completed_at IS NULL"
            ).fetchone()[0]
        )
    except sqlite3.Error:
        source_active_leases = -1
        blockers.append("source_job_leases_unreadable")

    try:
        jobs_by_status = {
            str(status): int(count)
            for status, count in sqlite_conn.execute(
                "SELECT status, COUNT(*) FROM jobs GROUP BY status"
            )
        }
    except sqlite3.Error:
        jobs_by_status = {}
        blockers.append("source_jobs_unreadable")

    queued = jobs_by_status.get("queued", 0)
    running = jobs_by_status.get("running", 0)
    # Match readiness/health_summary: only results belonging to active jobs.
    # Historical pending rows on completed/stopped jobs must not block cutover.
    try:
        pending_results = int(
            sqlite_conn.execute(
                """
                SELECT COUNT(*) FROM job_results r
                JOIN jobs j ON j.id = r.job_id
                WHERE r.progress_state = 'pending'
                  AND j.status IN ('queued', 'running')
                """
            ).fetchone()[0]
        )
    except sqlite3.Error:
        try:
            pending_results = int(
                sqlite_conn.execute(
                    """
                    SELECT COUNT(*) FROM job_results r
                    JOIN jobs j ON j.id = r.job_id
                    WHERE r.status = 'pending'
                      AND j.status IN ('queued', 'running')
                    """
                ).fetchone()[0]
            )
        except sqlite3.Error:
            pending_results = 0
    try:
        verifying_results = int(
            sqlite_conn.execute(
                """
                SELECT COUNT(*) FROM job_results r
                JOIN jobs j ON j.id = r.job_id
                WHERE r.progress_state = 'verifying'
                  AND j.status IN ('queued', 'running')
                """
            ).fetchone()[0]
        )
    except sqlite3.Error:
        try:
            verifying_results = int(
                sqlite_conn.execute(
                    """
                    SELECT COUNT(*) FROM job_results r
                    JOIN jobs j ON j.id = r.job_id
                    WHERE r.status = 'verifying'
                      AND j.status IN ('queued', 'running')
                    """
                ).fetchone()[0]
            )
        except sqlite3.Error:
            verifying_results = 0

    if queued or running or pending_results or verifying_results:
        blockers.append("active_work_present")
    if source_active_leases > 0 and not allow_active_leases:
        blockers.append("active_job_leases_present")
    elif source_active_leases > 0:
        warnings.append("active_job_leases_present_allowed_for_observation")

    source_summaries: dict[str, Any] = {}
    for name in tables:
        table = require_registered(name)
        if name not in source_table_set:
            source_summaries[name] = {
                "table": name,
                "count": 0,
                "key_digest": "",
                "content_digest": "",
                "verify_level": verify_level,
                "missing": True,
            }
        else:
            source_summaries[name] = summarize_sqlite_table(
                sqlite_conn, table, level=verify_level
            )

    financial_source = financial_summary_sqlite(sqlite_conn)

    target_active_leases = None
    rollback_ok = None
    target_summaries: dict[str, Any] = {}
    financial_target: dict[str, Any] = {}
    dsn_ok = False
    uses_tunnel = False

    if dsn is None:
        try:
            dsn = resolve_database_url(None)
        except Exception as exc:  # noqa: BLE001
            blockers.append(f"dsn_missing:{exc}")
            dsn = None

    if dsn:
        dsn_ok = True
        uses_tunnel = dsn_uses_local_tunnel(dsn)
        if not uses_tunnel:
            warnings.append("dsn_not_local_tunnel_15432")
        try:
            rollback_ok = write_rollback_probe(dsn)
            if not rollback_ok:
                blockers.append("target_rollback_write_test_failed")
        except Exception as exc:  # noqa: BLE001
            rollback_ok = False
            blockers.append(f"target_write_probe_error:{exc}")

        try:
            with pg_connection(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) AS n FROM job_leases WHERE completed_at IS NULL"
                    )
                    target_active_leases = int(cur.fetchone()["n"])
            if target_active_leases and target_active_leases > 0 and not allow_active_leases:
                blockers.append("target_active_job_leases_present")
        except Exception as exc:  # noqa: BLE001
            blockers.append(f"target_lease_query_error:{exc}")

        for name in tables:
            table = require_registered(name)
            try:
                tgt = summarize_pg_table(dsn, table, level=verify_level)
                target_summaries[name] = tgt
                src = source_summaries[name]
                mismatched = src["count"] != tgt["count"]
                if verify_level in {"keys", "full"}:
                    mismatched = mismatched or src.get("key_digest") != tgt.get("key_digest")
                if verify_level == "full":
                    mismatched = mismatched or src.get("content_digest") != tgt.get(
                        "content_digest"
                    )
                if mismatched:
                    differences[name] = {"source": src, "target": tgt}
            except Exception as exc:  # noqa: BLE001
                differences[name] = {"error": str(exc)}
                blockers.append(f"target_table_error:{name}")

        financial_tables = {
            "users",
            "credit_ledger",
            "payment_orders",
            "redemption_codes",
            "promo_credit_grants",
        }
        if financial_tables.issubset(set(tables)):
            try:
                financial_target = financial_summary_pg(dsn)
                if financial_source != financial_target:
                    differences["__financial__"] = {
                        "source": financial_source,
                        "target": financial_target,
                    }
                    blockers.append("financial_summary_mismatch")
            except Exception as exc:  # noqa: BLE001
                blockers.append(f"financial_target_error:{exc}")
        else:
            warnings.append("financial_check_skipped_partial_table_set")

    ready = not blockers and not differences
    return {
        "ready": ready,
        "blockers": blockers,
        "warnings": warnings,
        "differences": differences,
        "verify_level": verify_level,
        "source": {
            "sqlite": str(sqlite_path),
            "active_leases": source_active_leases,
            "jobs_by_status": jobs_by_status,
            "queued_jobs": queued,
            "running_jobs": running,
            "pending_results": pending_results,
            "verifying_results": verifying_results,
            "table_count": len(source_summaries),
            "financial": financial_source,
        },
        "target": {
            "dsn_configured": dsn_ok,
            "uses_local_tunnel": uses_tunnel,
            "active_leases": target_active_leases,
            "rollback_write_test": rollback_ok,
            "table_count": len(target_summaries),
            "financial": financial_target,
        },
        "tables": tables,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", required=True, type=Path)
    parser.add_argument("--postgres-dsn", default=None)
    parser.add_argument("--allow-active-leases", action="store_true")
    parser.add_argument("--tables", default="")
    parser.add_argument(
        "--verify-level",
        choices=("full", "keys", "counts"),
        default="full",
        help="full content digests (slow); keys=count+pk digest; counts=row counts only",
    )
    args = parser.parse_args()

    if args.tables.strip():
        tables = [t.strip() for t in args.tables.split(",") if t.strip()]
        for name in tables:
            require_registered(name)
    else:
        tables = list(all_registered_tables())

    report = build_report(
        sqlite_path=args.sqlite,
        dsn=args.postgres_dsn,
        allow_active_leases=args.allow_active_leases,
        tables=tables,
        verify_level=args.verify_level,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
