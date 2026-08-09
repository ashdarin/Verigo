"""Build the private Company Finder DuckDB catalogue from Parquet parts."""

from __future__ import annotations

from pathlib import Path

import duckdb

DATA_GLOB = "/opt/verigo-company-finder/data/parquet/*.parquet"
TARGET = Path("/opt/verigo-company-finder/data/company_catalog.duckdb")
TEMP = TARGET.with_suffix(".building.duckdb")

if TEMP.exists():
    TEMP.unlink()

with duckdb.connect(str(TEMP)) as connection:
    connection.execute("SET memory_limit = '768MB'")
    connection.execute("SET threads = 2")
    connection.execute("SET preserve_insertion_order = false")
    connection.execute(
        """CREATE TABLE companies AS
            SELECT * FROM read_parquet(?)
            ORDER BY country, industry, name_search, id""",
        [DATA_GLOB],
    )
    rows = connection.execute("SELECT count(*) FROM companies").fetchone()[0]
    print(f"Imported {rows:,} companies")

TEMP.replace(TARGET)
