"""Regression checks for paced, stratified Company Finder sampling."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import duckdb


root = Path(__file__).resolve().parent.parent
temp_dir = Path(tempfile.mkdtemp(prefix="verigo-vitality-sampler-"))
catalogue = temp_dir / "catalogue.duckdb"
vitality = temp_dir / "vitality.sqlite"
with duckdb.connect(str(catalogue)) as connection:
    connection.execute("""
        CREATE TABLE companies (
            id VARCHAR, name VARCHAR, name_search VARCHAR, website VARCHAR,
            linkedin_url VARCHAR, country VARCHAR, region VARCHAR, locality VARCHAR,
            industry VARCHAR, size VARCHAR, founded VARCHAR
        )
    """)
    rows = [(
        f"company-{index}", f"Company {index}", f"company {index}",
        f"company-{index}.example", "", f"country-{index % 12}", "", "",
        f"industry-{index % 20}", ("1-10", "11-50", "51-200")[index % 3], "2020",
    ) for index in range(1_200)]
    connection.executemany("INSERT INTO companies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)

os.environ.update({
    "COMPANY_FINDER_DATABASE_PATH": str(catalogue),
    "COMPANY_FINDER_VITALITY_DATABASE_PATH": str(vitality),
    "COMPANY_FINDER_SAMPLE_BURNIN_TARGET": "960",
    "COMPANY_FINDER_DAILY_SAMPLE_TARGET": "1000",
    "COMPANY_FINDER_SAMPLE_HARD_LIMIT": "1500",
    "COMPANY_FINDER_SAMPLE_INTERVAL_SECONDS": "900",
    "COMPANY_FINDER_SAMPLE_QUEUE_LIMIT": "100",
})
sys.path.insert(0, str(root / "deploy"))
script = root / "deploy" / "company-vitality-sampler.py"
spec = importlib.util.spec_from_file_location("company_vitality_sampler_test", script)
assert spec and spec.loader
sampler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sampler)

first = sampler.run()
assert first["status"] == "scheduled"
assert first["target"] == 960
assert first["batch"] == 10
assert first["inserted"] == 10

from company_vitality import VitalityStore, iso_at, utc_now  # noqa: E402

store = VitalityStore(vitality)
sampler_status = store.report(1)["sampler"]
assert sampler_status["mode"] == "burnin"
assert sampler_status["target_per_day"] == 960
assert sampler_status["scheduled_today"] == 10
with store.connect() as connection:
    queue = connection.execute(
        "SELECT source, priority, country FROM vitality_queue"
    ).fetchall()
assert len(queue) == 10
assert {tuple(row[:2]) for row in queue} == {("daily_sample", 200)}
assert len({row[2] for row in queue}) >= 3

while task := store.claim_next():
    assert task["source"] == "daily_sample"
    store.complete(task, {
        "state": "active_verified", "reason": "website_title_identity_match",
        "evidence_kind": "official_website_title", "evidence_strength": "strong",
        "checked_at": iso_at(), "review_duration_ms": 3_000,
    })
report = store.report(1)
assert report["totals"]["checks"] == 10
assert report["totals"]["sources"]["daily_sample"]["checks"] == 10
assert report["totals"]["sources"]["daily_sample"]["review_duration"] == {
    "average_ms": 3_000, "samples": 10,
}
serialized = json.dumps(report)
assert "company-" not in serialized

with sqlite3.connect(vitality) as connection:
    started = iso_at(utc_now() - timedelta(hours=25))
    connection.execute(
        "UPDATE vitality_sampler_meta SET value=?, updated_at=? WHERE key='started_at'",
        (started, started),
    )
sampler._seed = lambda _day: 1_234_567
stable = sampler.run()
assert stable["target"] == 1_000
assert stable["batch"] == 11
assert stable["inserted"] == 11
stable_status = store.report(1)["sampler"]
assert stable_status["mode"] == "stable"
assert stable_status["target_per_day"] == 1_000
assert stable_status["scheduled_today"] == 21

print("company vitality sampler smoke: ok")
