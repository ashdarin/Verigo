"""Regression checks for the Company Finder vitality shadow index."""

from __future__ import annotations

import json
import sys
import tempfile
import sqlite3
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploy"))

from company_vitality import VitalityStore, classify_page, iso_at


temp_dir = Path(tempfile.mkdtemp(prefix="verigo-company-vitality-"))
store = VitalityStore(temp_dir / "vitality.sqlite")
items = [{
    "id": "company-1",
    "name": "Example Corporation",
    "name_display": "Example Corporation",
    "website": "example.com",
    "website_domain": "example.com",
    "country": "germany",
}]

store.annotate_and_enqueue(items)
assert items[0]["vitality_state"] == "queued"
assert items[0]["vitality_queue_state"] == "queued"
assert store.stats()["queued"] == 1
with store.connect() as connection:
    assert tuple(connection.execute(
        "SELECT priority, country FROM vitality_queue WHERE company_id='company-1'"
    ).fetchone()) == (5, "germany")

task = store.claim_next()
assert task and task["company_id"] == "company-1"
assert task["source"] == "user_search"
store.complete(task, {
    "state": "active_verified",
    "confidence": 0.94,
    "reason": "website_title_identity_match",
    "evidence_kind": "official_website_title",
    "evidence_strength": "strong",
    "dns_status": "ok",
    "http_status": 200,
    "final_url": "https://example.com/",
    "page_title": "Example Corporation",
    "identity_score": 1.0,
    "checked_at": iso_at(),
})

store.annotate_and_enqueue(items)
assert items[0]["vitality_state"] == "active_verified"
assert items[0]["vitality_queue_state"] == ""
assert items[0]["vitality_reason"] == "website_title_identity_match"
assert store.stats()["states"] == {"active_verified": 1}
assert store.get("company-1")["evidence_kind"] == "official_website_title"

active = classify_page(
    "Example Corporation", "example.com",
    b"<html><head><title>Example Corporation</title></head><body>Products</body></html>",
    "text/html; charset=utf-8",
)
assert active["state"] == "active_verified"
assert float(active["identity_score"]) >= 0.45
assert active["evidence_kind"] == "official_website_title"

legal = classify_page(
    "Example GmbH", "example.de",
    b"<html><head><title>Example GmbH</title></head><body>Impressum Handelsregister HRB 42</body></html>",
    "text/html; charset=utf-8", "germany",
)
assert legal["state"] == "active_verified"
assert legal["evidence_kind"] == "official_website_legal"
assert legal["evidence_market"] == "germany"

content = classify_page(
    "Example Corporation", "different-domain.com",
    b"<html><head><title>Welcome</title></head><body>About Example Corporation and its products</body></html>",
    "text/html",
)
assert content["state"] == "active_verified"
assert content["evidence_kind"] == "official_website_content"

domain_only = classify_page(
    "Acme Labs", "acmelabs.com",
    b"<html><head><title>Welcome</title></head><body>Products and services</body></html>",
    "text/html",
)
assert domain_only["state"] == "recently_observed"
assert domain_only["evidence_kind"] == "official_website_domain"
assert domain_only["evidence_strength"] == "moderate"

grace_task = {
    "company_id": "company-grace", "domain": "grace.example",
    "normalized_name": "Grace Example", "country": "australia",
}
store.complete(grace_task, {
    "state": "active_verified", "confidence": 0.9,
    "reason": "website_title_identity_match", "evidence_kind": "official_website_title",
    "evidence_strength": "strong", "checked_at": iso_at(),
})
with store.connect() as connection:
    grace_row = dict(connection.execute(
        "SELECT * FROM company_vitality WHERE company_id='company-grace'"
    ).fetchone())
store.complete({
    **grace_task, "last_public_evidence_at": grace_row["last_public_evidence_at"],
    "consecutive_failures": grace_row["consecutive_failures"],
    "last_evidence_kind": grace_row["evidence_kind"],
    "last_evidence_strength": grace_row["evidence_strength"],
}, {"state": "uncertain", "reason": "website_identity_uncertain", "checked_at": iso_at()})
assert store.get("company-grace")["vitality_state"] == "recently_observed"
with store.connect() as connection:
    grace_row = dict(connection.execute(
        "SELECT * FROM company_vitality WHERE company_id='company-grace'"
    ).fetchone())
store.complete({
    **grace_task, "last_public_evidence_at": grace_row["last_public_evidence_at"],
    "consecutive_failures": grace_row["consecutive_failures"],
    "last_evidence_kind": grace_row["evidence_kind"],
    "last_evidence_strength": grace_row["evidence_strength"],
}, {"state": "uncertain", "reason": "website_identity_uncertain", "checked_at": iso_at()})
assert store.get("company-grace")["vitality_state"] == "uncertain"

parked = classify_page(
    "Example Corporation", "example.com",
    b"<html><title>Buy this domain</title><body>This domain is for sale</body></html>",
    "text/html",
)
assert parked["state"] == "inactive"
assert parked["reason"] == "parked_domain"

nxdomain_items = [{
    "id": "company-2", "name": "Gone Example", "name_display": "Gone Example",
    "website": "gone-example.invalid", "website_domain": "gone-example.invalid",
}]
store.annotate_and_enqueue(nxdomain_items)
task = store.claim_next()
assert task and task["company_id"] == "company-2"
store.complete(task, {
    "state": "inactive", "confidence": 0.95, "reason": "nxdomain",
    "dns_status": "nxdomain", "checked_at": iso_at(),
})
store.annotate_and_enqueue(nxdomain_items)
assert nxdomain_items[0]["vitality_state"] == "uncertain"
with store.connect() as connection:
    connection.execute(
        "UPDATE company_vitality SET next_check_at = ? WHERE company_id = ?",
        ("2000-01-01T00:00:00+00:00", "company-2"),
    )
assert store.enqueue_due() == 1
task = store.claim_next()
assert task and task["company_id"] == "company-2"
store.complete(task, {
    "state": "inactive", "confidence": 0.95, "reason": "nxdomain",
    "dns_status": "nxdomain", "checked_at": iso_at(),
})
store.annotate_and_enqueue(nxdomain_items)
assert nxdomain_items[0]["vitality_state"] == "inactive"

with store.connect() as connection:
    connection.execute(
        """UPDATE company_vitality
        SET evidence_kind='legacy_website_identity', evidence_strength='strong',
            reason='website_identity_match',
            next_check_at='2099-01-01T00:00:00+00:00'
        WHERE company_id='company-1'"""
    )
assert store.enqueue_due() == 1
with store.connect() as connection:
    assert connection.execute(
        "SELECT priority FROM vitality_queue WHERE company_id='company-1'"
    ).fetchone()[0] == 30
store.annotate_and_enqueue(items)
with store.connect() as connection:
    assert connection.execute(
        "SELECT priority FROM vitality_queue WHERE company_id='company-1'"
    ).fetchone()[0] == 5
assert store.claim_next()["company_id"] == "company-1"
assert store.release_claims() == 1
with store.connect() as connection:
    assert connection.execute(
        "SELECT claimed_at FROM vitality_queue WHERE company_id='company-1'"
    ).fetchone()[0] is None
    connection.execute("DELETE FROM vitality_queue WHERE company_id='company-1'")
    connection.execute(
        """UPDATE company_vitality SET reason='connection_failed',
            next_check_at='2099-01-01T00:00:00+00:00' WHERE company_id='company-1'"""
    )
assert store.enqueue_due() == 0

legacy_path = temp_dir / "legacy.sqlite"
with sqlite3.connect(legacy_path) as connection:
    connection.execute("""CREATE TABLE company_vitality (
        company_id TEXT PRIMARY KEY, domain TEXT NOT NULL, normalized_name TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL DEFAULT 'queued', confidence REAL NOT NULL DEFAULT 0,
        dns_status TEXT NOT NULL DEFAULT '', http_status INTEGER, final_url TEXT NOT NULL DEFAULT '',
        page_title TEXT NOT NULL DEFAULT '', is_parked INTEGER NOT NULL DEFAULT 0,
        identity_score REAL NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT 'not_checked',
        checked_at TEXT, last_public_evidence_at TEXT, consecutive_failures INTEGER NOT NULL DEFAULT 0,
        next_check_at TEXT, updated_at TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE vitality_queue (
        company_id TEXT PRIMARY KEY, domain TEXT NOT NULL, normalized_name TEXT NOT NULL DEFAULT '',
        priority INTEGER NOT NULL DEFAULT 100, attempts INTEGER NOT NULL DEFAULT 0,
        available_at TEXT NOT NULL, claimed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
legacy_store = VitalityStore(legacy_path)
with legacy_store.connect() as connection:
    vitality_columns = {row[1] for row in connection.execute("PRAGMA table_info(company_vitality)")}
    queue_columns = {row[1] for row in connection.execute("PRAGMA table_info(vitality_queue)")}
assert {"country", "evidence_kind", "evidence_strength"}.issubset(vitality_columns)
assert {"country", "source"}.issubset(queue_columns)

quality_store = VitalityStore(temp_dir / "quality.sqlite")
quality_items = [{
    "id": "private-company-reference", "name": "Private Example",
    "website_domain": "private-example.test", "country": "germany",
}]
quality_store.annotate_and_enqueue(quality_items)
quality_task = quality_store.claim_next()
assert quality_task and "queue_wait_ms" in quality_task
quality_store.complete(quality_task, {
    "state": "active_verified", "confidence": 0.95,
    "reason": "website_title_identity_match",
    "evidence_kind": "official_website_title", "evidence_strength": "strong",
    "checked_at": iso_at(), "review_duration_ms": 1250,
})
quality_store.complete({
    "company_id": "private-company-reference", "domain": "private-example.test",
    "normalized_name": "Private Example", "country": "germany",
    "state": "active_verified", "last_evidence_kind": "official_website_title",
    "last_evidence_strength": "strong", "last_public_evidence_at": iso_at(),
}, {
    "state": "inactive", "reason": "parked_domain", "checked_at": iso_at(),
})
quality_report = quality_store.report(14)
assert len(quality_report["daily"]) == 14
assert quality_report["totals"]["checks"] == 2
assert quality_report["totals"]["outcomes"]["active_verified"] == 1
assert quality_report["totals"]["outcomes"]["inactive"] == 1
assert quality_report["totals"]["transitions"] == {
    "visible_to_hidden": 1, "hidden_to_visible": 1, "net_public": 0,
}
assert quality_report["totals"]["queue_wait"]["samples"] == 1
assert quality_report["totals"]["review_duration"] == {
    "average_ms": 1250, "p95_ms": 2000, "p99_ms": 2000,
    "percentile_samples": 1, "samples": 1,
}
assert quality_report["totals"]["sources"]["user_search"]["queue_wait"]["p95_ms"] is not None
assert quality_report["totals"]["sources"]["user_search"]["review_duration"]["p99_ms"] == 2000
serialized_report = json.dumps(quality_report)
assert "private-company-reference" not in serialized_report
assert "private-example.test" not in serialized_report
assert "Private Example" not in serialized_report

percentile_store = VitalityStore(temp_dir / "percentiles.sqlite")
for index in range(100):
    percentile_store.complete({
        "company_id": f"percentile-{index}", "domain": f"percentile-{index}.test",
        "normalized_name": "Percentile", "source": "daily_sample",
    }, {
        "state": "active_verified", "reason": "website_title_identity_match",
        "checked_at": iso_at(), "review_duration_ms": 900 if index < 95 else 10_000,
    })
percentile_duration = percentile_store.report(1)["totals"]["review_duration"]
assert percentile_duration["p95_ms"] == 1_000
assert percentile_duration["p99_ms"] == 15_000
assert percentile_duration["percentile_samples"] == 100

with quality_store.connect() as connection:
    connection.execute("DROP TABLE vitality_daily_sources")
migrated_quality_store = VitalityStore(quality_store.path)
assert migrated_quality_store.report(14)["totals"]["sources"]["scheduled_refresh"]["checks"] == 2

sample_store = VitalityStore(temp_dir / "samples.sqlite")
sample_items = [{
    "id": f"sample-{index}", "name": f"Sample {index}",
    "website_domain": f"sample-{index}.example", "country": "germany",
} for index in range(3)]
sample_result = sample_store.enqueue_samples(
    sample_items, daily_limit=2, max_batch=2, queue_limit=10,
)
assert sample_result["inserted"] == 2
assert sample_store.sample_day_scheduled() == 2
with sample_store.connect() as connection:
    assert {
        tuple(row) for row in connection.execute(
            "SELECT source, priority FROM vitality_queue"
        ).fetchall()
    } == {("daily_sample", 200)}
sample_store.annotate_and_enqueue([sample_items[0]])
with sample_store.connect() as connection:
    assert tuple(connection.execute(
        "SELECT source, priority FROM vitality_queue WHERE company_id='sample-0'"
    ).fetchone()) == ("user_search", 5)
while sample_task := sample_store.claim_next():
    sample_store.complete(sample_task, {
        "state": "active_verified", "reason": "website_title_identity_match",
        "evidence_kind": "official_website_title", "evidence_strength": "strong",
        "checked_at": iso_at(), "review_duration_ms": 900,
    })
sample_report = sample_store.report(1)
assert sample_report["totals"]["sources"]["user_search"]["checks"] == 1
assert sample_report["totals"]["sources"]["daily_sample"]["checks"] == 1
assert sample_store.enqueue_samples(
    [sample_items[2]], daily_limit=2, max_batch=1, queue_limit=10,
)["inserted"] == 0

print("company vitality smoke: ok")
