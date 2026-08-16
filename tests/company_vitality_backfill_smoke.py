"""Regression checks for country-only vitality metadata backfill."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import duckdb


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))

from company_vitality import VitalityStore  # noqa: E402
from company_vitality_backfill import backfill  # noqa: E402


temp_dir = Path(tempfile.mkdtemp(prefix="verigo-vitality-backfill-"))
catalogue = temp_dir / "catalogue.duckdb"
vitality = temp_dir / "vitality.sqlite"
with duckdb.connect(str(catalogue)) as connection:
    connection.execute("CREATE TABLE companies(id VARCHAR, country VARCHAR)")
    connection.execute(
        "INSERT INTO companies VALUES ('company-active', 'germany'), ('company-inactive', 'australia')"
    )

store = VitalityStore(vitality)
store.complete(
    {"company_id": "company-active", "domain": "active.example", "normalized_name": "Active"},
    {
        "state": "active_verified", "reason": "website_title_identity_match",
        "evidence_kind": "official_website_title", "evidence_strength": "strong",
    },
)
store.complete(
    {"company_id": "company-inactive", "domain": "inactive.example", "normalized_name": "Inactive"},
    {"state": "inactive", "reason": "parked_domain"},
)

summary = backfill(vitality, catalogue)
assert summary == {"updated": 2, "scheduled": 1}
with store.connect() as connection:
    active = connection.execute(
        "SELECT country, next_check_at FROM company_vitality WHERE company_id='company-active'"
    ).fetchone()
    inactive = connection.execute(
        "SELECT country FROM company_vitality WHERE company_id='company-inactive'"
    ).fetchone()
assert active["country"] == "germany"
assert active["next_check_at"]
assert inactive["country"] == "australia"
assert backfill(vitality, catalogue) == {"updated": 0, "scheduled": 0}

print("company vitality backfill smoke: ok")
