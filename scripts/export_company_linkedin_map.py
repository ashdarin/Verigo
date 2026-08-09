"""Export the source CSV's stable company ID to LinkedIn URL mapping."""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--memory-limit", default="768MB")
    args = parser.parse_args()
    if not args.source.is_file():
        raise SystemExit(f"Source file does not exist: {args.source}")
    args.output.parent.mkdir(parents=True, exist_ok=True)

    import duckdb

    source = str(args.source).replace("'", "''")
    output = str(args.output).replace("'", "''")
    with duckdb.connect() as connection:
        connection.execute(f"SET memory_limit = '{args.memory_limit}'")
        connection.execute("SET threads = 2")
        connection.execute("SET preserve_insertion_order = false")
        connection.execute(
            f"""COPY (
                SELECT trim(id)::VARCHAR AS id, trim(linkedin_url)::VARCHAR AS linkedin_url
                FROM read_csv('{source}', header=true, all_varchar=true, strict_mode=false)
                WHERE trim(id) <> ''
            ) TO '{output}' (FORMAT PARQUET, COMPRESSION ZSTD,
                             ROW_GROUP_SIZE 100000, OVERWRITE_OR_IGNORE true)"""
        )
        rows = connection.execute(
            f"SELECT count(*) FROM read_parquet('{output}')"
        ).fetchone()[0]
    print(f"Created {args.output} with {rows:,} rows ({args.output.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
