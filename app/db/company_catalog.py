"""Read-only access to the separately managed company search catalogue."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import settings


class CompanyCatalogUnavailable(RuntimeError):
    """Raised when no configured company catalogue backend is available."""


_TINYBIRD_FIELDS = (
    "country", "industry", "region", "size", "query", "has_website", "offset", "limit",
)
_PUBLIC_FIELDS = (
    "id", "name", "website", "linkedin_url", "logo_url", "country", "region",
    "locality", "industry", "size", "founded",
)


def _hunter_logo_url(website: object) -> str:
    value = str(website or "").strip()
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"https://{value}")
    hostname = (parsed.hostname or "").lower().removeprefix("www.")
    return f"https://logos.hunter.io/{hostname}" if hostname else ""


def _linkedin_url(value: object) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    hostname = (parsed.hostname or "").lower()
    if hostname not in {"linkedin.com", "www.linkedin.com"}:
        return ""
    return parsed.geturl()


def _tinybird_enabled() -> bool:
    return bool(settings.company_catalog_tinybird_url and settings.company_catalog_tinybird_token)


def _service_enabled() -> bool:
    return bool(settings.company_catalog_service_url and settings.company_catalog_service_token)


def _service_search(**filters: object) -> tuple[int, list[dict[str, object]], bool]:
    params: dict[str, str] = {}
    for name in _TINYBIRD_FIELDS:
        value = filters.get(name)
        if value is None or value == "":
            continue
        params[name] = str(value).lower() if name == "has_website" else str(value)

    try:
        response = httpx.get(
            f"{settings.company_catalog_service_url}/search",
            params=params,
            headers={"Authorization": f"Bearer {settings.company_catalog_service_token}"},
            timeout=settings.company_catalog_service_timeout_seconds,
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise CompanyCatalogUnavailable("Company Finder is temporarily unavailable") from exc

    items = [
        {field: row.get(field, "") for field in _PUBLIC_FIELDS}
        for row in payload.get("items", [])
        if isinstance(row, dict)
    ]
    for item in items:
        item["linkedin_url"] = _linkedin_url(item["linkedin_url"])
    return int(payload.get("total", len(items))), items, bool(payload.get("has_more"))


def _tinybird_search(**filters: object) -> tuple[int, list[dict[str, object]], bool]:
    params: dict[str, str] = {}
    for name in _TINYBIRD_FIELDS:
        value = filters.get(name)
        if value is None or value == "":
            continue
        params[name] = str(value).lower() if name == "has_website" else str(value)

    try:
        response = httpx.get(
            f"{settings.company_catalog_tinybird_url}/v0/pipes/company_search.json",
            params=params,
            headers={"Authorization": f"Bearer {settings.company_catalog_tinybird_token}"},
            timeout=settings.company_catalog_tinybird_timeout_seconds,
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise CompanyCatalogUnavailable("Company Finder is temporarily unavailable") from exc

    items = [
        {field: row.get(field, "") for field in _PUBLIC_FIELDS}
        for row in payload.get("data", [])
        if isinstance(row, dict)
    ]
    offset = int(filters["offset"])
    lower_bound = int(payload.get("rows_before_limit_at_least", len(items) + offset))
    return max(lower_bound, len(items) + offset), items, lower_bound > offset + len(items)


def _connection():
    if not settings.company_catalog_enabled:
        raise CompanyCatalogUnavailable("Company Finder MVP is not enabled")
    if not settings.company_catalog_path.is_file():
        raise CompanyCatalogUnavailable("Company catalogue has not been imported yet")
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - dependency is explicit
        raise CompanyCatalogUnavailable("Company Finder dependency is not installed") from exc

    connection = duckdb.connect(str(settings.company_catalog_path), read_only=True)
    connection.execute(f"SET memory_limit = '{settings.company_catalog_query_memory_limit}'")
    connection.execute("SET threads = 2")
    return connection


def _filters(
    *, country: str | None, industry: str | None, region: str | None, size: str | None,
    query: str | None, has_website: bool | None,
) -> tuple[str, list[object]]:
    clauses: list[str] = []
    values: list[object] = []
    for column, value in (("country", country), ("industry", industry), ("region", region), ("size", size)):
        if value:
            clauses.append(f"{column} = ?")
            values.append(value.strip().lower())
    if query:
        clauses.append("name_search LIKE ?")
        values.append(f"%{query.strip().lower()}%")
    if has_website is True:
        clauses.append("website <> ''")
    elif has_website is False:
        clauses.append("website = ''")
    return (f" WHERE {' AND '.join(clauses)}" if clauses else ""), values


def search(
    *, country: str | None = None, industry: str | None = None, region: str | None = None,
    size: str | None = None, query: str | None = None, has_website: bool | None = None,
    offset: int = 0, limit: int = 25,
) -> tuple[int, list[dict[str, object]], bool]:
    if _service_enabled():
        return _service_search(
            country=country, industry=industry, region=region, size=size, query=query,
            has_website=has_website, offset=offset, limit=limit,
        )
    if _tinybird_enabled():
        return _tinybird_search(
            country=country, industry=industry, region=region, size=size, query=query,
            has_website=has_website, offset=offset, limit=limit,
        )
    where, values = _filters(
        country=country, industry=industry, region=region, size=size, query=query,
        has_website=has_website,
    )
    with _connection() as connection:
        total = int(connection.execute(f"SELECT COUNT(*) FROM companies{where}", values).fetchone()[0])
        rows = connection.execute(
            f"""SELECT id, name, website, '' AS linkedin_url, country, region, locality, industry, size, founded
                FROM companies{where}
                ORDER BY name_search, id
                LIMIT ? OFFSET ?""",
            [*values, limit, offset],
        ).fetchall()
    fields = ("id", "name", "website", "linkedin_url", "country", "region", "locality", "industry", "size", "founded")
    items = [dict(zip(fields, row, strict=True)) for row in rows]
    for item in items:
        item["logo_url"] = _hunter_logo_url(item["website"])
        item["linkedin_url"] = _linkedin_url(item["linkedin_url"])
    return total, items, offset + len(items) < total


def facets(column: str, *, country: str | None = None, industry: str | None = None) -> list[dict[str, object]]:
    if _service_enabled():
        raise CompanyCatalogUnavailable("Company Finder facets are not available")
    if column not in {"country", "industry", "region", "size"}:
        raise ValueError("Unsupported company catalogue facet")
    filters = {"country": country, "industry": industry}
    filters[column] = None
    where, values = _filters(region=None, size=None, query=None, has_website=None, **filters)
    conjunction = " AND " if where else " WHERE "
    with _connection() as connection:
        rows: Sequence[tuple[object, object]] = connection.execute(
            f"""SELECT {column}, COUNT(*) AS count FROM companies{where}{conjunction}{column} <> '' GROUP BY {column}
                ORDER BY count DESC, {column} ASC LIMIT 500""",
            values,
        ).fetchall()
    return [{"value": str(value), "count": int(count)} for value, count in rows]


def status() -> dict[str, object]:
    if _service_enabled():
        total, _, _ = _service_search(offset=0, limit=1)
        return {"total": total, "backend": "company-finder-service"}
    if _tinybird_enabled():
        total, _, _ = _tinybird_search(offset=0, limit=1)
        return {"total": total, "backend": "tinybird"}
    with _connection() as connection:
        total = int(connection.execute("SELECT COUNT(*) FROM companies").fetchone()[0])
    return {"total": total, "backend": "duckdb"}
