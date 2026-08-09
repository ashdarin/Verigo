"""Private, read-only Company Finder API for the dedicated catalogue node."""

from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Annotated

import duckdb
from fastapi import Depends, FastAPI, Header, HTTPException, Query

DATA_GLOB = os.getenv("COMPANY_FINDER_PARQUET_GLOB", "/opt/verigo-company-finder/data/parquet/*.parquet")
DATABASE_PATH = os.getenv("COMPANY_FINDER_DATABASE_PATH", "/opt/verigo-company-finder/data/company_catalog.duckdb")
SERVICE_TOKEN = os.getenv("COMPANY_FINDER_SERVICE_TOKEN", "")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
    if not SERVICE_TOKEN or not authorization or not authorization.startswith("Bearer " ):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not hmac.compare_digest(authorization.removeprefix("Bearer "), SERVICE_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")


def search(
    _: Annotated[None, Depends(require_token)],
    country: str | None = Query(default=None, max_length=80),
    industry: str | None = Query(default=None, max_length=160),
    region: str | None = Query(default=None, max_length=160),
    size: str | None = Query(default=None, max_length=40),
    query: str | None = Query(default=None, max_length=120),
    has_website: str | None = Query(default=None, pattern="^(true|false)$"),
    offset: int = Query(default=0, ge=0, le=5000),
    limit: int = Query(default=25, ge=1, le=50),
) -> dict[str, object]:
    clauses: list[str] = []
    values: list[object] = []
    for field, value in (("country", country), ("industry", industry), ("region", region), ("size", size)):
        if value:
            clauses.append(f"{field} = ?")
            values.append(value.strip().lower())
    if query:
        clauses.append("name_search LIKE ?")
        values.append(f"%{query.strip().lower()}%")
    if has_website == "true":
        clauses.append("website <> ''")
    elif has_website == "false":
        clauses.append("website = ''")

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    fields = ("id", "name", "website", "country", "region", "locality", "industry", "size", "founded")
    database_exists = Path(DATABASE_PATH).is_file()
    source = "companies" if database_exists else "read_parquet(?)"
    source_values: list[object] = [] if database_exists else [DATA_GLOB]
    with duckdb.connect(DATABASE_PATH if database_exists else ":memory:", read_only=database_exists) as connection:
        connection.execute("SET memory_limit = '768MB'")
        connection.execute("SET threads = 2")
        connection.execute("SET preserve_insertion_order = false")
        rows = connection.execute(
            f"""SELECT {', '.join(fields)}
                FROM {source} {where}
                ORDER BY name_search, id
                LIMIT ? OFFSET ?""",
            [*source_values, *values, limit + 1, offset],
        ).fetchall()

    has_more = len(rows) > limit
    items = [dict(zip(fields, row, strict=True)) for row in rows[:limit]]
    return {"total": offset + len(items) + (1 if has_more else 0), "items": items, "has_more": has_more}


@app.get("/health")
def health(_: Annotated[None, Depends(require_token)]) -> dict[str, str]:
    if not Path(DATABASE_PATH).is_file() and not list(Path(DATA_GLOB.rsplit("/", 1)[0]).glob("*.parquet")):
        raise HTTPException(status_code=503, detail="Company data is not ready")
    return {"status": "ok"}


app.get("/search")(search)
