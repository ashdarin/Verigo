"""Contract checks for privacy-preserving Company Finder product metrics."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.routes import require_company_finder_user, router  # noqa: E402
from app.api.schemas import CompanyFinderAnalyticsRequest  # noqa: E402
from app.db.auth import User  # noqa: E402
from app.db.metrics import MetricsStore, metrics_store  # noqa: E402


def store_contract() -> None:
    with tempfile.TemporaryDirectory(prefix="verigo-company-metrics-") as raw_dir:
        database = Path(raw_dir) / "metrics.sqlite"

        def connect():
            return sqlite3.connect(database, isolation_level=None)

        store = MetricsStore()
        store._connect = connect  # type: ignore[method-assign]
        with patch("app.db.metrics.postgres_active", return_value=False):
            store.initialize()
            with closing(connect()) as connection:
                connection.execute(
                    """CREATE TABLE jobs (
                        created_at TEXT NOT NULL, stop_on_deliverable INTEGER NOT NULL,
                        emails_json TEXT NOT NULL, parent_id TEXT, execution_target TEXT NOT NULL
                    )"""
                )
            store.record_company_finder_event("company_detail_open", "user-1")
            store.record_company_finder_event("company_detail_open", "user-1")
            store.record_company_finder_event("company_detail_open", "user-2")
            store.record_company_finder_event("company_website_open", "user-1")
            usage = store.feature_usage()["company_finder"]

        detail = usage["totals"]["company_detail_open"]
        assert detail == {"count": 3, "unique_users": 2}
        assert usage["totals"]["company_website_open"] == {
            "count": 1, "unique_users": 1,
        }
        with closing(connect()) as connection:
            columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(company_finder_event_users)"
                )
            }
            hashes = [
                row[0] for row in connection.execute(
                    "SELECT user_hash FROM company_finder_event_users"
                )
            ]
        assert columns == {"day", "event_type", "user_hash"}
        assert len(hashes) == 3
        assert all(len(value) == 64 and "user-" not in value for value in hashes)


def api_contract() -> None:
    verified = User(
        id="metrics-user", username="metrics-user", email="metrics@example.com",
        created_at="2026-08-16T00:00:00+00:00", email_verified=True,
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_company_finder_user] = lambda: verified
    with patch.object(metrics_store, "record_company_finder_event") as record:
        with TestClient(app) as client:
            response = client.post(
                "/api/company-finder/analytics", json={"event": "company_detail_open"},
            )
            assert response.status_code == 204, response.text
            record.assert_called_once_with("company_detail_open", "metrics-user")
            assert client.post(
                "/api/company-finder/analytics", json={"event": "not-an-event"},
            ).status_code == 422
            assert client.post(
                "/api/company-finder/analytics",
                json={"event": "company_website_open", "domain": "example.com"},
            ).status_code == 422
    try:
        CompanyFinderAnalyticsRequest.model_validate(
            {"event": "company_website_open", "company_id": "company-1"}
        )
        raise AssertionError("sensitive catalogue fields were accepted")
    except ValidationError:
        pass


def main() -> int:
    store_contract()
    api_contract()
    print("company finder metrics smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
