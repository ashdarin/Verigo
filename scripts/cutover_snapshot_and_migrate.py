#!/usr/bin/env python3
"""Create a consistent SQLite snapshot and migrate registered tables to PostgreSQL.

On the app host (after loading postgres.env):

  PYTHONPATH=/opt/verigo/current /opt/verigo/.venv/bin/python \\
    scripts/cutover_snapshot_and_migrate.py \\
    --sqlite /opt/verigo/data/verigo.db \\
    --snapshot /var/backups/verigo/cutover-final.db \\
    --include-all-tables --recreate-schema --batch-size 1000
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def snapshot_sqlite(source: Path, target: Path) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst)
    with sqlite3.connect(f"file:{target}?mode=ro", uri=True) as check:
        result = check.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise SystemExit(f"snapshot quick_check failed: {result}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--postgres-dsn", default=None)
    parser.add_argument("--include-all-tables", action="store_true")
    parser.add_argument("--tables", default="")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--recreate-schema", action="store_true")
    parser.add_argument("--no-prune", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-unknown-source-tables", action="store_true")
    parser.add_argument("--snapshot-only", action="store_true")
    args = parser.parse_args()

    print(
        json.dumps(
            {
                "phase": "snapshot_begin",
                "source": str(args.sqlite),
                "target": str(args.snapshot),
            }
        ),
        flush=True,
    )
    quick = snapshot_sqlite(args.sqlite, args.snapshot)
    print(
        json.dumps(
            {"phase": "snapshot_ok", "quick_check": quick, "path": str(args.snapshot)}
        ),
        flush=True,
    )
    if args.snapshot_only:
        return 0

    migrate = ROOT / "scripts" / "migrate_sqlite_to_postgres.py"
    cmd = [
        sys.executable,
        str(migrate),
        "--sqlite",
        str(args.snapshot),
        "--batch-size",
        str(args.batch_size),
    ]
    if args.postgres_dsn:
        cmd += ["--postgres-dsn", args.postgres_dsn]
    if args.include_all_tables:
        cmd.append("--include-all-tables")
    if args.tables:
        cmd += ["--tables", args.tables]
    if args.recreate_schema:
        cmd.append("--recreate-schema")
    if args.no_prune:
        cmd.append("--no-prune")
    if args.dry_run:
        cmd.append("--dry-run")
    if args.allow_unknown_source_tables:
        cmd.append("--allow-unknown-source-tables")

    print(json.dumps({"phase": "migrate_spawn", "cmd": cmd[1:]}), flush=True)
    return subprocess.call(cmd, cwd=str(ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
