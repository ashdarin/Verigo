#!/usr/bin/env python3
"""Migrate registered Verigo tables from SQLite to PostgreSQL."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.postgres_migrate import (  # noqa: E402
    ensure_schema,
    financial_summary_pg,
    financial_summary_sqlite,
    iter_sqlite_rows,
    open_sqlite,
    sqlite_tables,
    summarize_pg_table,
    summarize_sqlite_table,
    upsert_table_stream,
)
from app.db.postgres_schema import (  # noqa: E402
    CORE_JOBSTORE_TABLES,
    TABLES,
    all_registered_tables,
    require_registered,
)
from app.db.postgresql import resolve_database_url  # noqa: E402


def emit(phase: str, **payload: Any) -> None:
    print(json.dumps({"phase": phase, **payload}, ensure_ascii=False, default=str), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", required=True, type=Path)
    parser.add_argument("--postgres-dsn", default=None)
    parser.add_argument("--include-all-tables", action="store_true")
    parser.add_argument("--tables", default="")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--recreate-schema", action="store_true")
    parser.add_argument("--no-prune", action="store_true")
    parser.add_argument("--allow-unknown-source-tables", action="store_true")
    parser.add_argument(
        "--verify-level",
        choices=("full", "keys", "counts"),
        default="full",
        help="full=count+key+content digests; keys=count+key; counts=row counts only",
    )
    args = parser.parse_args()

    if not args.sqlite.exists():
        emit("error", message=f"sqlite file not found: {args.sqlite}")
        return 2

    if args.tables.strip():
        table_names = [t.strip() for t in args.tables.split(",") if t.strip()]
    elif args.include_all_tables:
        table_names = list(all_registered_tables())
    else:
        table_names = [t for t in CORE_JOBSTORE_TABLES if t in TABLES]

    for name in table_names:
        require_registered(name)

    sqlite_conn = open_sqlite(args.sqlite)
    source_tables = sqlite_tables(sqlite_conn)
    unknown = sorted(source_tables - set(TABLES))
    if unknown and not args.allow_unknown_source_tables:
        emit("error", message="unregistered SQLite tables present", unknown_tables=unknown)
        return 2

    verify_level = args.verify_level
    for name in table_names:
        table = require_registered(name)
        if name not in source_tables:
            emit(
                "source",
                table=name,
                count=0,
                key_digest="",
                content_digest="",
                missing=True,
                verify_level=verify_level,
            )
        else:
            emit(
                "source",
                **summarize_sqlite_table(sqlite_conn, table, level=verify_level),
            )

    emit("financial_source", **financial_summary_sqlite(sqlite_conn))

    if args.dry_run:
        emit("verify", ok=True, dry_run=True, tables=len(table_names), verify_level=verify_level)
        return 0

    dsn = resolve_database_url(args.postgres_dsn)
    ensure_schema(dsn, table_names, recreate=args.recreate_schema)

    for name in table_names:
        table = require_registered(name)
        if name not in source_tables:
            result = upsert_table_stream(
                dsn,
                table,
                iter(()),
                batch_size=max(1, args.batch_size),
                prune=not args.no_prune,
                known_count=0,
            )
            emit("migrate", **result)
            continue
        result = upsert_table_stream(
            dsn,
            table,
            iter_sqlite_rows(sqlite_conn, table),
            batch_size=max(1, args.batch_size),
            prune=not args.no_prune,
        )
        emit("migrate", **result)

    differences: dict[str, Any] = {}
    for name in table_names:
        table = require_registered(name)
        if name not in source_tables:
            src = {
                "table": name,
                "count": 0,
                "key_digest": "",
                "content_digest": "",
                "verify_level": verify_level,
            }
        else:
            src = summarize_sqlite_table(sqlite_conn, table, level=verify_level)
        tgt = summarize_pg_table(dsn, table, level=verify_level)
        emit("target", **tgt)
        mismatched = src["count"] != tgt["count"]
        if verify_level in {"keys", "full"}:
            mismatched = mismatched or src["key_digest"] != tgt["key_digest"]
        if verify_level == "full":
            mismatched = mismatched or src["content_digest"] != tgt["content_digest"]
        if mismatched:
            differences[name] = {"source": src, "target": tgt}

    financial_tables = {
        "users",
        "credit_ledger",
        "payment_orders",
        "redemption_codes",
        "promo_credit_grants",
    }
    if financial_tables.issubset(set(table_names)):
        fin_src = financial_summary_sqlite(sqlite_conn)
        fin_tgt = financial_summary_pg(dsn)
        emit("financial_target", **fin_tgt)
        if fin_src != fin_tgt:
            differences["__financial__"] = {"source": fin_src, "target": fin_tgt}
    else:
        emit(
            "financial_target",
            skipped=True,
            reason="financial tables not all included in this run",
        )

    ok = not differences
    emit("verify", ok=ok, differences=differences)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
