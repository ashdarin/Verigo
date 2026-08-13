"""Inventory helper: list registered PostgreSQL schema tables and emit DDL.

Does not connect to production. Safe for local/CI use.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.postgres_schema import (  # noqa: E402
    TABLES,
    all_registered_tables,
    render_full_schema_sql,
    schema_summary,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ddl", action="store_true", help="Print full CREATE DDL")
    parser.add_argument("--json", action="store_true", help="Print schema summary JSON")
    args = parser.parse_args()

    if args.ddl:
        print(render_full_schema_sql())
        return 0
    if args.json:
        print(json.dumps(schema_summary(), indent=2, ensure_ascii=False))
        return 0

    summary = schema_summary()
    print(f"registered_tables={summary['table_count']}")
    for group, names in summary["groups"].items():
        print(f"[{group}] {len(names)}")
        for name in names:
            t = TABLES[name]
            print(
                f"  {name}: cols={len(t.columns)} pk={list(t.primary_key) or '-'} "
                f"uq={len(t.uniques)} ix={len(t.indexes)} fk={len(t.foreign_keys)}"
            )
    missing_in_groups = set(all_registered_tables()) - {
        n for names in summary["groups"].values() for n in names
    }
    if missing_in_groups:
        print("ERROR ungrouped:", sorted(missing_in_groups))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
