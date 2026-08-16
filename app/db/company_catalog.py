"""Read-only access to the separately managed company search catalogue."""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from app.config import settings
from app.core.company_catalog_normalization import (
    canonical_filter_value,
    normalize_company_record,
    normalize_facet_items,
)


class CompanyCatalogUnavailable(RuntimeError):
    """Raised when no configured company catalogue backend is available."""


class CompanyCatalogNotFound(LookupError):
    """Raised when a company is not public or has no eligible vitality evidence."""


MAX_SEARCH_WINDOW = 100


_TINYBIRD_FIELDS = (
    "country", "industry", "region", "size", "query", "has_website", "offset", "limit",
)
_PUBLIC_FIELDS = (
    "id", "name", "website", "linkedin_url", "logo_url", "country", "region",
    "locality", "industry", "size", "founded", "vitality_state", "vitality_queue_state",
    "vitality_confidence", "vitality_checked_at", "vitality_last_public_evidence_at",
    "vitality_reason",
)


def _hunter_logo_url(website: object) -> str:
    value = str(website or "").strip()
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"https://{value}")
    hostname = (parsed.hostname or "").lower().removeprefix("www.")
    return f"https://logos.hunter.io/{hostname}" if hostname else ""


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
    items = [_with_logo(normalize_company_record(item)) for item in items]
    offset = int(filters["offset"])
    has_more = bool(payload.get("has_more")) and offset + len(items) < MAX_SEARCH_WINDOW
    return min(MAX_SEARCH_WINDOW, int(payload.get("total", len(items)))), items, has_more


def search_public(**filters: object) -> dict[str, object]:
    """Return only companies with positive, recent public vitality evidence.

    The dedicated catalogue service owns the vitality cache and queue. Keeping
    this filter there prevents the application server from exposing a raw
    catalogue page while a website is still being checked.
    """
    if not _service_enabled():
        raise CompanyCatalogUnavailable("Company Finder is temporarily unavailable")

    params: dict[str, str] = {"visibility": "public"}
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
    items = [_with_logo(normalize_company_record(item)) for item in items]
    total = min(MAX_SEARCH_WINDOW, int(payload.get("total", len(items))))
    return {
        "total": total,
        "items": items,
        "has_more": bool(payload.get("has_more")),
        "pending_count": max(0, int(payload.get("pending_count", 0))),
        "refresh_after_seconds": max(0, min(10, int(payload.get("refresh_after_seconds", 0)))),
    }


def public_company_detail(company_id: str) -> dict[str, object]:
    """Read one public company detail through the dedicated catalogue service."""
    if not _service_enabled():
        raise CompanyCatalogUnavailable("Company Finder is temporarily unavailable")
    try:
        response = httpx.get(
            f"{settings.company_catalog_service_url}/companies/{quote(company_id, safe='')}",
            headers={"Authorization": f"Bearer {settings.company_catalog_service_token}"},
            timeout=settings.company_catalog_service_timeout_seconds,
        )
        if response.status_code == 404:
            raise CompanyCatalogNotFound(company_id)
        response.raise_for_status()
        payload = response.json()
    except CompanyCatalogNotFound:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise CompanyCatalogUnavailable("Company Finder is temporarily unavailable") from exc
    if not isinstance(payload, dict) or not payload.get("id"):
        raise CompanyCatalogUnavailable("Company Finder returned an invalid company detail")
    return _with_logo(normalize_company_record(payload))


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
    items = [_with_logo(normalize_company_record(item)) for item in items]
    offset = int(filters["offset"])
    lower_bound = int(payload.get("rows_before_limit_at_least", len(items) + offset))
    total = min(MAX_SEARCH_WINDOW, max(lower_bound, len(items) + offset))
    has_more = lower_bound > offset + len(items) and offset + len(items) < MAX_SEARCH_WINDOW
    return total, items, has_more


def _with_logo(item: dict[str, object]) -> dict[str, object]:
    if not item.get("logo_url"):
        item["logo_url"] = _hunter_logo_url(item.get("website_url") or item.get("website"))
    return item


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
    for column, value in (("country", country), ("industry", industry), ("size", size)):
        if value:
            clauses.append(f"{column} = ?")
            values.append(value.strip().lower())
    if region:
        clauses.append("region LIKE ?")
        values.append(f"%{region.strip().lower()}%")
    if query:
        clauses.append("(name_search LIKE ? OR website LIKE ?)")
        search_term = f"%{query.strip().lower()}%"
        values.extend((search_term, search_term))
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
    country = canonical_filter_value("country", country)
    industry = canonical_filter_value("industry", industry)
    size = canonical_filter_value("size", size)
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
        if query:
            total = 0
            fetch_limit = limit + 1
            order_by = ""
        else:
            total = int(connection.execute(f"SELECT COUNT(*) FROM companies{where}", values).fetchone()[0])
            fetch_limit = limit
            order_by = "ORDER BY name_search, id"
        rows = connection.execute(
            f"""SELECT id, name, website, linkedin_url, country, region, locality, industry, size, founded
                FROM companies{where}
                {order_by}
                LIMIT ? OFFSET ?""",
            [*values, fetch_limit, offset],
        ).fetchall()
    has_more = len(rows) > limit if query else offset + len(rows) < total
    rows = rows[:limit]
    if query:
        total = offset + len(rows) + (1 if has_more else 0)
    fields = ("id", "name", "website", "linkedin_url", "country", "region", "locality", "industry", "size", "founded")
    items = [dict(zip(fields, row, strict=True)) for row in rows]
    items = [_with_logo(normalize_company_record(item)) for item in items]
    return min(MAX_SEARCH_WINDOW, total), items, has_more and offset + len(items) < MAX_SEARCH_WINDOW


def facets(column: str, *, country: str | None = None, industry: str | None = None) -> list[dict[str, object]]:
    if _service_enabled():
        return _service_facets(column, country=country, industry=industry)
    if column not in {"country", "industry", "region", "size"}:
        raise ValueError("Unsupported company catalogue facet")
    filters = {
        "country": canonical_filter_value("country", country),
        "industry": canonical_filter_value("industry", industry),
    }
    filters[column] = None
    where, values = _filters(region=None, size=None, query=None, has_website=None, **filters)
    conjunction = " AND " if where else " WHERE "
    with _connection() as connection:
        rows: Sequence[tuple[object, object]] = connection.execute(
            f"""SELECT {column}, COUNT(*) AS count FROM companies{where}{conjunction}{column} <> '' GROUP BY {column}
                ORDER BY count DESC, {column} ASC LIMIT 500""",
            values,
        ).fetchall()
    return normalize_facet_items(column, [{"value": str(value), "count": int(count)} for value, count in rows])


@lru_cache(maxsize=64)
def _service_facets(column: str, *, country: str | None = None, industry: str | None = None) -> list[dict[str, object]]:
    if column not in {"country", "industry", "region", "size"}:
        raise ValueError("Unsupported company catalogue facet")
    params: dict[str, str] = {}
    if country:
        params["country"] = canonical_filter_value("country", country)
    if industry:
        params["industry"] = canonical_filter_value("industry", industry)
    try:
        response = httpx.get(
            f"{settings.company_catalog_service_url}/facets/{column}",
            params=params,
            headers={"Authorization": f"Bearer {settings.company_catalog_service_token}"},
            timeout=settings.company_catalog_service_timeout_seconds,
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise CompanyCatalogUnavailable("Company Finder facets are temporarily unavailable") from exc
    return normalize_facet_items(column, [
        row for row in payload.get("items", []) if isinstance(row, dict)
    ])


def status() -> dict[str, object]:
    if _service_enabled():
        try:
            response = httpx.get(
                f"{settings.company_catalog_service_url}/health",
                headers={"Authorization": f"Bearer {settings.company_catalog_service_token}"},
                timeout=settings.company_catalog_service_timeout_seconds,
            )
            response.raise_for_status()
            health: dict[str, object] = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise CompanyCatalogUnavailable("Company Finder is temporarily unavailable") from exc
        return {
            "total": 0,
            "backend": "company-finder-service",
            "healthy": health.get("status") == "ok",
            "vitality": health.get("vitality", {}),
        }
    if _tinybird_enabled():
        total, _, _ = _tinybird_search(offset=0, limit=1)
        return {"total": total, "backend": "tinybird"}
    with _connection() as connection:
        total = int(connection.execute("SELECT COUNT(*) FROM companies").fetchone()[0])
    return {"total": total, "backend": "duckdb"}


def quality_report(days: int = 14) -> dict[str, object]:
    """Read privacy-preserving vitality aggregates from the catalogue node."""
    if not _service_enabled():
        raise CompanyCatalogUnavailable("Company Finder quality report is temporarily unavailable")
    try:
        response = httpx.get(
            f"{settings.company_catalog_service_url}/vitality/report",
            params={"days": max(1, min(90, int(days)))},
            headers={"Authorization": f"Bearer {settings.company_catalog_service_token}"},
            timeout=settings.company_catalog_service_timeout_seconds,
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise CompanyCatalogUnavailable("Company Finder quality report is temporarily unavailable") from exc
    if not isinstance(payload.get("daily"), list) or not isinstance(payload.get("totals"), dict):
        raise CompanyCatalogUnavailable("Company Finder returned an invalid quality report")
    return payload
