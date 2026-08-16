"""Regression checks for Company Finder normalization and local fallback reads."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import duckdb
from fastapi import HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.core.company_catalog_normalization import (
    INDUSTRY_LABELS,
    country_label,
    normalize_company_name,
    normalize_linkedin_url,
    normalize_website,
)
from app.db import company_catalog
from app.db.auth import User
from app.api.routes import company_catalog_quality, require_company_finder_user


assert len(INDUSTRY_LABELS) == 147
assert normalize_company_name("  !! boost-your-sales !  ") == "Boost-Your-Sales"
assert normalize_company_name("atac engineering, inc.") == "Atac Engineering, Inc."
assert normalize_company_name('"zhongshan yishun metal co., ltd.') == "Zhongshan Yishun Metal Co., Ltd."
assert country_label("united states") == "美国"
assert country_label("democratic republic of the congo") == "刚果民主共和国"
assert normalize_website("Example.COM/about/?utm_source=test&id=4#team") == (
    "https://example.com/about?id=4", "example.com",
)
assert normalize_website("javascript:alert(1)") == ("", "")
assert normalize_linkedin_url("de.linkedin.com/company/example/?trk=test") == (
    "https://www.linkedin.com/company/example"
)
assert normalize_linkedin_url("linkedin.com/in/example") == ""
verified_user = User(id="user-1", username="user", email="user@example.com", created_at="2026-08-16T00:00:00+00:00", email_verified=True)
assert require_company_finder_user(verified_user) is verified_user
try:
    require_company_finder_user(User(id="user-2", username="user", email="user@example.com", created_at="2026-08-16T00:00:00+00:00"))
    raise AssertionError("an unverified Company Finder user was accepted")
except HTTPException as exc:
    assert exc.status_code == 403

temp_dir = Path(tempfile.mkdtemp(prefix="verigo-company-catalogue-"))
catalogue = temp_dir / "companies.duckdb"
with duckdb.connect(str(catalogue)) as connection:
    connection.execute("""
        CREATE TABLE companies (
            id VARCHAR, name VARCHAR, name_search VARCHAR, website VARCHAR,
            linkedin_url VARCHAR, country VARCHAR, region VARCHAR, locality VARCHAR,
            industry VARCHAR, size VARCHAR, founded VARCHAR
        )
    """)
    connection.execute("""
        INSERT INTO companies VALUES (
            'company-1', '! boost-your-sales !', '! boost-your-sales !',
            'boost-your-sales.eu', 'linkedin.com/company/boost-your-sales',
            'germany', 'north rhine-westphalia', 'oberhausen',
            'management consulting', '51-200', '2019'
        )
    """)
    connection.execute("""
        INSERT INTO companies VALUES (
            'company-2', 'Example Holding', 'example holding',
            'boost-your-sales.com', '', 'united states', 'california',
            'san francisco', 'computer software', '11-50', '2020'
        )
    """)

previous = {
    "company_catalog_enabled": settings.company_catalog_enabled,
    "company_catalog_path": settings.company_catalog_path,
    "company_catalog_service_url": settings.company_catalog_service_url,
    "company_catalog_service_token": settings.company_catalog_service_token,
    "company_catalog_tinybird_url": settings.company_catalog_tinybird_url,
    "company_catalog_tinybird_token": settings.company_catalog_tinybird_token,
}
try:
    object.__setattr__(settings, "company_catalog_enabled", True)
    object.__setattr__(settings, "company_catalog_path", catalogue)
    object.__setattr__(settings, "company_catalog_service_url", "")
    object.__setattr__(settings, "company_catalog_service_token", "")
    object.__setattr__(settings, "company_catalog_tinybird_url", "")
    object.__setattr__(settings, "company_catalog_tinybird_token", "")

    total, items, has_more = company_catalog.search(country="德国", industry="管理咨询")
    assert total == 1 and not has_more
    item = items[0]
    assert item["name_display"] == "Boost-Your-Sales"
    assert item["location_label"] == "德国 · 北莱茵-威斯特法伦州 · Oberhausen"
    assert item["industry_label"] == "管理咨询"
    assert item["size_label"] == "51–200 人"
    assert item["website_url"] == "https://boost-your-sales.eu"
    assert item["linkedin_url"] == "https://www.linkedin.com/company/boost-your-sales"
    assert company_catalog.facets("country") == [
        {"value": "germany", "count": 1, "label": "德国"},
        {"value": "united states", "count": 1, "label": "美国"},
    ]
    total, items, has_more = company_catalog.search(query="boost-your-sales")
    assert total == 2 and len(items) == 2 and not has_more
finally:
    for name, value in previous.items():
        object.__setattr__(settings, name, value)

os.environ["COMPANY_FINDER_DATABASE_PATH"] = str(catalogue)
os.environ["COMPANY_FINDER_SERVICE_TOKEN"] = "catalogue-test-token"
os.environ["COMPANY_FINDER_VITALITY_DATABASE_PATH"] = str(temp_dir / "vitality.sqlite")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "core"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploy"))
service_path = Path(__file__).resolve().parent.parent / "deploy" / "company-finder-service.py"
spec = importlib.util.spec_from_file_location("company_finder_service_test", service_path)
assert spec and spec.loader
service_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service_module)
with TestClient(service_module.app) as client:
    headers = {"Authorization": "Bearer catalogue-test-token"}
    response = client.get("/vitality/report")
    assert response.status_code == 401, response.text
    response = client.get("/search", headers=headers)
    assert response.status_code == 422, response.text
    response = client.get(
        "/search", params={"query": "example", "offset": 90, "limit": 25}, headers=headers,
    )
    assert response.status_code == 422, response.text
    response = client.get("/search", params={"country": "germany", "limit": 25}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["country_label"] == "德国"
    assert response.json()["items"][0]["vitality_state"] == "queued"
    assert response.json()["items"][0]["vitality_reason"] == "not_checked"
    response = client.get(
        "/search", params={"country": "germany", "visibility": "public"}, headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []
    assert response.json()["pending_count"] == 1
    response = client.get("/companies/company-1", headers=headers)
    assert response.status_code == 404, response.text
    response = client.get("/companies/company-2", headers=headers)
    assert response.status_code == 404, response.text
    service_module.vitality_store.complete(
        {"company_id": "company-1", "domain": "boost-your-sales.eu", "normalized_name": "Boost-Your-Sales", "country": "germany"},
        {
            "state": "active_verified", "reason": "website_legal_identity_match",
            "confidence": 0.95, "evidence_kind": "official_website_legal",
            "evidence_strength": "strong", "page_title": "Boost Your Sales",
        },
    )
    response = client.get("/vitality/report", params={"days": 7}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["days"] == 7
    assert response.json()["totals"]["checks"] == 1
    assert "company-1" not in response.text
    assert "boost-your-sales.eu" not in response.text
    response = client.get(
        "/search", params={"country": "germany", "visibility": "public"}, headers=headers,
    )
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == ["company-1"]
    assert response.json()["pending_count"] == 0
    response = client.get("/companies/company-1", headers=headers)
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["name_display"] == "Boost-Your-Sales"
    assert detail["vitality_state"] == "active_verified"
    assert detail["vitality_evidence"]["kind"] == "official_website_legal"
    assert detail["vitality_evidence"]["type"] == "德国官网法律信息与公司身份相符"
    assert detail["vitality_evidence"]["source"] == "official_website"
    response = client.get("/facets/industry", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["items"] == [
        {"value": "computer software", "count": 1, "label": "计算机软件"},
        {"value": "management consulting", "count": 1, "label": "管理咨询"},
    ]
    response = client.get("/facets/industry", headers=headers)
    assert response.status_code == 200
    assert service_module._facet_rows.cache_info().hits >= 1
    response = client.get(
        "/search", params={"query": "boost-your-sales", "limit": 25}, headers=headers,
    )
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == ["company-1", "company-2"]

quality_payload = {"days": 7, "daily": [], "totals": {}, "current": {}}
quality_response = Mock()
quality_response.raise_for_status.return_value = None
quality_response.json.return_value = quality_payload
previous_service_url = settings.company_catalog_service_url
previous_service_token = settings.company_catalog_service_token
try:
    object.__setattr__(settings, "company_catalog_service_url", "http://catalogue.test")
    object.__setattr__(settings, "company_catalog_service_token", "private-token")
    with patch.object(company_catalog.httpx, "get", return_value=quality_response) as request:
        assert company_catalog.quality_report(7) == quality_payload
        assert request.call_args.kwargs["params"] == {"days": 7}
        assert request.call_args.kwargs["headers"] == {"Authorization": "Bearer private-token"}
finally:
    object.__setattr__(settings, "company_catalog_service_url", previous_service_url)
    object.__setattr__(settings, "company_catalog_service_token", previous_service_token)

with patch.object(company_catalog, "quality_report", return_value=quality_payload) as quality:
    assert company_catalog_quality(verified_user, days=7) == quality_payload
    quality.assert_called_once_with(7)

print("company catalogue smoke: ok")
