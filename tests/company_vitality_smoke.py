"""Regression checks for the Company Finder vitality shadow index."""

from __future__ import annotations

import sys
import tempfile
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
}]

store.annotate_and_enqueue(items)
assert items[0]["vitality_state"] == "queued"
assert items[0]["vitality_queue_state"] == "queued"
assert store.stats()["queued"] == 1

task = store.claim_next()
assert task and task["company_id"] == "company-1"
store.complete(task, {
    "state": "active_verified",
    "confidence": 0.94,
    "reason": "website_identity_match",
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
assert items[0]["vitality_reason"] == "website_identity_match"
assert store.stats()["states"] == {"active_verified": 1}

active = classify_page(
    "Example Corporation", "example.com",
    b"<html><head><title>Example Corporation</title></head><body>Products</body></html>",
    "text/html; charset=utf-8",
)
assert active["state"] == "active_verified"
assert float(active["identity_score"]) >= 0.45

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

print("company vitality smoke: ok")
