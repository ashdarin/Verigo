"""Build the local Company Finder MVP catalogue without loading the CSV into RAM."""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Import the company CSV into a separate DuckDB catalogue")
    parser.add_argument("--source", type=Path, required=True, help="Source UTF-8 CSV")
    parser.add_argument("--database", type=Path, required=True, help="Output DuckDB database")
    parser.add_argument("--memory-limit", default="1GB")
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()
    if not args.source.is_file():
        raise SystemExit(f"Source file does not exist: {args.source}")
    args.database.parent.mkdir(parents=True, exist_ok=True)

    import duckdb

    with duckdb.connect(str(args.database)) as connection:
        connection.execute(f"SET memory_limit = '{args.memory_limit}'")
        connection.execute(f"SET threads = {max(1, args.threads)}")
        connection.execute("SET preserve_insertion_order = false")
        connection.execute("DROP TABLE IF EXISTS companies")
        connection.execute(
            """CREATE TABLE companies AS
            SELECT
                trim(id)::VARCHAR AS id,
                trim(name)::VARCHAR AS name,
                lower(trim(name))::VARCHAR AS name_search,
                lower(trim(website))::VARCHAR AS website,
                trim(linkedin_url)::VARCHAR AS linkedin_url,
                lower(trim(country))::VARCHAR AS country,
                lower(trim(region))::VARCHAR AS region,
                lower(trim(locality))::VARCHAR AS locality,
                lower(trim(industry))::VARCHAR AS industry,
                lower(trim(size))::VARCHAR AS size,
                trim(founded)::VARCHAR AS founded
            FROM read_csv(?, header=true, all_varchar=true, strict_mode=false)
            WHERE trim(id) <> '' AND trim(name) <> ''
            """,
            [str(args.source)],
        )
        # Keep the MVP catalogue columnar and append-only. Building B-tree
        # indexes for 35m short rows exceeds a small workstation's memory and
        # is unnecessary for the bounded, faceted reads used by the MVP.
        total = connection.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        duplicate_ids = connection.execute("SELECT COUNT(*) - COUNT(DISTINCT id) FROM companies").fetchone()[0]
        print(f"Imported {total:,} companies; duplicate source IDs: {duplicate_ids:,}")
        print(f"Database: {args.database}")


if __name__ == "__main__":
    main()
