"""Private, read-only Company Finder API for the dedicated catalogue node."""

from __future__ import annotations

import hmac
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

import duckdb
from fastapi import Depends, FastAPI, Header, HTTPException, Query

from company_catalog_normalization import (
    canonical_filter_value,
    normalize_company_record,
    normalize_facet_items,
)
from company_vitality import VitalityStore

DATA_GLOB = os.getenv("COMPANY_FINDER_PARQUET_GLOB", "/opt/verigo-company-finder/data/parquet/*.parquet")
DATABASE_PATH = os.getenv("COMPANY_FINDER_DATABASE_PATH", "/opt/verigo-company-finder/data/company_catalog.duckdb")
SERVICE_TOKEN = os.getenv("COMPANY_FINDER_SERVICE_TOKEN", "")
VITALITY_DATABASE_PATH = os.getenv("COMPANY_FINDER_VITALITY_DATABASE_PATH", "")
MAX_SEARCH_WINDOW = 100
FIELDS = (
    "id", "name", "website", "linkedin_url", "country", "region",
    "locality", "industry", "size", "founded",
)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
try:
    vitality_store = VitalityStore(VITALITY_DATABASE_PATH) if VITALITY_DATABASE_PATH else None
except Exception:
    # Vitality runs in shadow mode and must never take catalogue search down.
    vitality_store = None


def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
    if not SERVICE_TOKEN or not authorization or not authorization.startswith("Bearer " ):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not hmac.compare_digest(authorization.removeprefix("Bearer "), SERVICE_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")


def hunter_logo_url(website: object) -> str:
    value = str(website or "").strip()
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"https://{value}")
    hostname = (parsed.hostname or "").lower().removeprefix("www.")
    return f"https://logos.hunter.io/{hostname}" if hostname else ""


def linkedin_url(value: object) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    hostname = (parsed.hostname or "").lower()
    if hostname not in {"linkedin.com", "www.linkedin.com"}:
        return ""
    return parsed.geturl()


def public_evidence_payload(
    vitality: dict[str, object], company: dict[str, object],
) -> dict[str, object]:
    kind = str(vitality.get("evidence_kind") or "legacy_website_identity")
    presentations = {
        "official_website_legal": (
            "官网法律信息与公司身份相符",
            "官网同时包含公司身份和所在市场常见的企业登记或法律信息。",
        ),
        "official_website_title": (
            "官网标题与公司身份相符",
            "官网页面标题与公司名称一致，并且网站可以公开访问。",
        ),
        "official_website_content": (
            "官网公开内容与公司身份相符",
            "官网正文中的公司身份信息与目录记录一致。",
        ),
        "official_website_domain": (
            "官网域名与公司品牌相符",
            "网站可以公开访问，域名与公司品牌一致；该证据会更频繁复核。",
        ),
        "legacy_website_identity": (
            "官网内容与公司身份相符",
            "此前官网检查确认了公司身份，系统正在按新证据标准复核。",
        ),
    }
    evidence_type, summary = presentations.get(kind, presentations["legacy_website_identity"])
    market = str(company.get("country_label") or "")
    if kind == "official_website_legal" and market:
        evidence_type = f"{market}官网法律信息与公司身份相符"
    return {
        "kind": kind,
        "type": evidence_type,
        "summary": summary,
        "source": "official_website",
        "strength": str(vitality.get("evidence_strength") or "strong"),
        "market": market,
        "observed_at": vitality.get("vitality_last_public_evidence_at") or "",
        "reason": vitality["vitality_reason"],
        "dns_status": vitality["dns_status"],
        "http_status": vitality["http_status"],
        "final_url": vitality["final_url"],
        "page_title": vitality["page_title"],
        "identity_score": vitality["identity_score"],
    }


def search(
    _: Annotated[None, Depends(require_token)],
    country: str | None = Query(default=None, max_length=80),
    industry: str | None = Query(default=None, max_length=160),
    region: str | None = Query(default=None, max_length=160),
    size: str | None = Query(default=None, max_length=40),
    query: str | None = Query(default=None, max_length=120),
    has_website: str | None = Query(default=None, pattern="^(true|false)$"),
    visibility: str = Query(default="internal", pattern="^(internal|public)$"),
    offset: int = Query(default=0, ge=0, lt=MAX_SEARCH_WINDOW),
    limit: int = Query(default=25, ge=1, le=50),
) -> dict[str, object]:
    meaningful_filters = (query, country, region, industry, size)
    if not any(value and value.strip() for value in meaningful_filters):
        raise HTTPException(status_code=422, detail="A company query or filter is required")
    if offset + limit > MAX_SEARCH_WINDOW:
        raise HTTPException(status_code=422, detail="Only the first 100 companies are available")
    if has_website is None:
        has_website = "true"
    country = canonical_filter_value("country", country)
    industry = canonical_filter_value("industry", industry)
    size = canonical_filter_value("size", size)
    clauses: list[str] = []
    values: list[object] = []
    for field, value in (("country", country), ("industry", industry), ("size", size)):
        if value:
            clauses.append(f"{field} = ?")
            values.append(value.strip().lower())
    if region:
        clauses.append("region LIKE ?")
        values.append(f"%{region.strip().lower()}%")
    if has_website == "true":
        clauses.append("website <> ''")
    elif has_website == "false":
        clauses.append("website = ''")

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    database_exists = Path(DATABASE_PATH).is_file()
    source = "companies" if database_exists else "read_parquet(?)"
    source_values: list[object] = [] if database_exists else [DATA_GLOB]
    # Public search never paginates through the raw catalogue. It examines a
    # fixed candidate window, queues any cold vitality records, then exposes
    # only companies with recent positive public evidence.
    public_search = visibility == "public"
    raw_limit = MAX_SEARCH_WINDOW if public_search else offset + limit + 1
    with duckdb.connect(DATABASE_PATH if database_exists else ":memory:", read_only=database_exists) as connection:
        connection.execute("SET memory_limit = '768MB'")
        connection.execute("SET threads = 2")
        connection.execute("SET preserve_insertion_order = false")
        if query:
            # Return name matches first, then domain-only matches. Reading a
            # bounded prefix lets DuckDB stop as soon as the requested page is
            # filled instead of scanning both columns across the full table.
            search_term = f"%{query.strip().lower()}%"
            requested_end = raw_limit
            conjunction = " AND " if where else " WHERE "
            name_rows = connection.execute(
                f"""SELECT {', '.join(FIELDS)} FROM {source}{where}{conjunction}name_search LIKE ?
                    LIMIT ?""",
                [*source_values, *values, search_term, requested_end],
            ).fetchall()
            combined_rows = list(name_rows)
            if len(combined_rows) < requested_end:
                combined_rows.extend(connection.execute(
                    f"""SELECT {', '.join(FIELDS)} FROM {source}{where}{conjunction}
                        website LIKE ? AND name_search NOT LIKE ? LIMIT ?""",
                    [
                        *source_values, *values, search_term, search_term,
                        requested_end - len(combined_rows),
                    ],
                ).fetchall())
            rows = combined_rows if public_search else combined_rows[offset:requested_end]
        else:
            rows = connection.execute(
                f"""SELECT {', '.join(FIELDS)}
                    FROM {source} {where}
                    ORDER BY name_search, id
                    LIMIT ? OFFSET ?""",
                [*source_values, *values, raw_limit, 0 if public_search else offset],
            ).fetchall()

    has_more = len(rows) > limit and offset + limit < MAX_SEARCH_WINDOW
    items = [dict(zip(FIELDS, row, strict=True)) for row in rows[:raw_limit]]
    items = [normalize_company_record(item) for item in items]
    for item in items:
        item["logo_url"] = hunter_logo_url(item.get("website_url") or item.get("website"))
    if vitality_store is not None:
        try:
            vitality_store.annotate_and_enqueue(items)
        except Exception:
            pass
    for item in items:
        item.setdefault("vitality_state", "unchecked")
        item.setdefault("vitality_queue_state", "")
        item.setdefault("vitality_confidence", 0.0)
        item.setdefault("vitality_checked_at", "")
        item.setdefault("vitality_last_public_evidence_at", "")
        item.setdefault("vitality_reason", "not_checked")
    if public_search:
        eligible_states = {"active_verified", "recently_observed"}
        pending_states = {"unchecked", "queued", "checking"}
        pending_count = sum(
            1 for item in items
            if item.get("vitality_state") in pending_states
            or item.get("vitality_queue_state") in {"queued", "checking"}
        )
        visible_items = [item for item in items if item.get("vitality_state") in eligible_states]
        page_items = visible_items[offset:offset + limit]
        has_more = offset + len(page_items) < len(visible_items)
        return {
            "total": len(visible_items),
            "items": page_items,
            "has_more": has_more,
            "pending_count": pending_count,
            # The two-worker shadow checker ordinarily drains a fresh page in
            # seconds. The client caps retries and leaves control with users.
            "refresh_after_seconds": 4 if pending_count else 0,
        }

    total = min(MAX_SEARCH_WINDOW, offset + len(items) + (1 if has_more else 0))
    return {"total": total, "items": items, "has_more": has_more}


@app.get("/companies/{company_id}")
def company_detail(
    company_id: str,
    _: Annotated[None, Depends(require_token)],
) -> dict[str, object]:
    """Return a public detail only after the vitality gate has passed."""
    company_id = company_id.strip()
    if not company_id or len(company_id) > 200:
        raise HTTPException(status_code=404, detail="Company not found")

    database_exists = Path(DATABASE_PATH).is_file()
    source = "companies" if database_exists else "read_parquet(?)"
    source_values: list[object] = [] if database_exists else [DATA_GLOB]
    with duckdb.connect(
        DATABASE_PATH if database_exists else ":memory:", read_only=database_exists,
    ) as connection:
        connection.execute("SET memory_limit = '256MB'")
        connection.execute("SET threads = 2")
        row = connection.execute(
            f"SELECT {', '.join(FIELDS)} FROM {source} WHERE id = ? LIMIT 1",
            [*source_values, company_id],
        ).fetchone()
    if row is None or vitality_store is None:
        raise HTTPException(status_code=404, detail="Company not found")

    item = normalize_company_record(dict(zip(FIELDS, row, strict=True)))
    item["logo_url"] = hunter_logo_url(item.get("website_url") or item.get("website"))
    vitality = vitality_store.get(company_id)
    if vitality is None or vitality.get("vitality_state") not in {"active_verified", "recently_observed"}:
        # Do not reveal whether an internal or inactive record exists.
        raise HTTPException(status_code=404, detail="Company not found")
    item.update({
        key: vitality[key]
        for key in (
            "vitality_state", "vitality_queue_state", "vitality_confidence",
            "vitality_checked_at", "vitality_last_public_evidence_at", "vitality_reason",
        )
    })
    item["vitality_evidence"] = public_evidence_payload(vitality, item)
    return item


@lru_cache(maxsize=64)
def _facet_rows(facet: str, country: str, industry: str) -> tuple[tuple[str, int], ...]:
    clauses: list[str] = []
    values: list[object] = []
    for field, value in (("country", country), ("industry", industry)):
        if value and field != facet:
            clauses.append(f"{field} = ?")
            values.append(value.strip().lower())
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    database_exists = Path(DATABASE_PATH).is_file()
    source = "companies" if database_exists else "read_parquet(?)"
    source_values: list[object] = [] if database_exists else [DATA_GLOB]
    conjunction = " AND " if where else " WHERE "
    with duckdb.connect(DATABASE_PATH if database_exists else ":memory:", read_only=database_exists) as connection:
        connection.execute("SET memory_limit = '768MB'")
        connection.execute("SET threads = 2")
        connection.execute("SET preserve_insertion_order = false")
        rows = connection.execute(
            f"""SELECT {facet}, count(*) AS count FROM {source}{where}{conjunction}{facet} <> ''
                GROUP BY {facet} ORDER BY count DESC, {facet} ASC LIMIT 500""",
            [*source_values, *values],
        ).fetchall()
    return tuple((str(value), int(count)) for value, count in rows)


@app.get("/facets/{facet}")
def facets(
    facet: str,
    _: Annotated[None, Depends(require_token)],
    country: str | None = Query(default=None, max_length=80),
    industry: str | None = Query(default=None, max_length=160),
) -> dict[str, object]:
    if facet not in {"country", "industry", "region", "size"}:
        raise HTTPException(status_code=422, detail="Unsupported company catalogue facet")
    country = canonical_filter_value("country", country)
    industry = canonical_filter_value("industry", industry)
    items = normalize_facet_items(facet, [
        {"value": value, "count": count}
        for value, count in _facet_rows(facet, country, industry)
    ])
    return {"items": items}


@app.get("/health")
def health(_: Annotated[None, Depends(require_token)]) -> dict[str, object]:
    if not Path(DATABASE_PATH).is_file() and not list(Path(DATA_GLOB.rsplit("/", 1)[0]).glob("*.parquet")):
        raise HTTPException(status_code=503, detail="Company data is not ready")
    vitality: dict[str, object] = {"enabled": False}
    if vitality_store is not None:
        try:
            vitality = vitality_store.stats()
        except Exception:
            vitality = {"enabled": True, "status": "degraded"}
    return {"status": "ok", "vitality": vitality}


@app.get("/vitality/stats")
def vitality_stats(_: Annotated[None, Depends(require_token)]) -> dict[str, object]:
    if vitality_store is None:
        return {"enabled": False}
    return vitality_store.stats()


@app.get("/vitality/report")
def vitality_report(
    _: Annotated[None, Depends(require_token)],
    days: int = Query(default=14, ge=1, le=90),
) -> dict[str, object]:
    if vitality_store is None:
        return {"enabled": False}
    return vitality_store.report(days)


app.get("/search")(search)
