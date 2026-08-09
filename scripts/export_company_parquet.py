"""Export the company catalogue to compressed, country-partitioned Parquet."""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True, help="Source CSV")
    parser.add_argument("--output", type=Path, required=True, help="Output directory")
    parser.add_argument("--memory-limit", default="768MB")
    args = parser.parse_args()
    if not args.source.is_file():
        raise SystemExit(f"Source file does not exist: {args.source}")
    args.output.mkdir(parents=True, exist_ok=True)
    output_file = args.output / "companies.parquet"

    import duckdb

    with duckdb.connect() as connection:
        connection.execute(f"SET memory_limit = '{args.memory_limit}'")
        connection.execute("SET threads = 2")
        connection.execute("SET preserve_insertion_order = false")
        source_sql = str(args.source).replace("'", "''")
        output_sql = str(output_file).replace("'", "''")
        connection.execute(
            f"""COPY (
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
                FROM read_csv('{source_sql}', header=true, all_varchar=true, strict_mode=false)
                WHERE trim(id) <> '' AND trim(name) <> ''
            ) TO '{output_sql}' (FORMAT PARQUET, COMPRESSION ZSTD,
                    ROW_GROUP_SIZE 100000, OVERWRITE_OR_IGNORE true)""",
        )
    files = list(args.output.rglob("*.parquet"))
    print(f"Created {len(files):,} Parquet files in {args.output}")
    print(f"Total bytes: {sum(path.stat().st_size for path in files):,}")


if __name__ == "__main__":
    main()
