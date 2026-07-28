from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path


temp_dir = Path(tempfile.mkdtemp(prefix="verigo-prospecting-beta-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
os.environ["VERIGO_DATABASE_PATH"] = str(temp_dir / "verigo.db")
os.environ["VERIGO_RESULTS_DIR"] = str(temp_dir / "results")
os.environ["VERIGO_NAME_CATALOG_PATH"] = str(temp_dir / "name_catalog.db")
os.environ["VERIGO_SECURE_COOKIES"] = "false"
os.environ["VERIGO_MAX_PENDING_JOBS"] = "50"
os.environ["VERIGO_PROSPECTING_BETA_ENABLED"] = "true"
os.environ["VERIGO_PROSPECTING_BETA_ALLOWED_EMAILS"] = "4671793@qq.com"
os.environ["VERIGO_PROSPECTING_BETA_MAX_CANDIDATES"] = "128"
os.environ["VERIGO_PROSPECTING_BETA_CATALOGUE_CANDIDATES"] = "128"

from fastapi.testclient import TestClient

from app.config import settings
from app.core.prospecting import (
    COUNTRY_PROFILES,
    ROLE_LOCAL_PARTS,
    generate_candidates,
    infer_email_pattern,
    normalize_company_domain,
)
from app.db.auth import auth_store
from app.db.jobs import job_store, utc_now
from app.db.prospecting import prospecting_store
from app.tasks.verification import normalize_result
from app.main import app


assert normalize_company_domain("https://www.example.com/about") == "example.com"
assert len(generate_candidates("example.com", "DE", 1_000)) == 1_000
german_candidates = generate_candidates("example.com", "DE", 128, requested_pattern="last.first")
assert len(german_candidates) == 128
first_personal = german_candidates[len(ROLE_LOCAL_PARTS)]
assert first_personal.pattern == "last.first"
assert first_personal.source == "user_selected_pattern"
assert {candidate.pattern for candidate in german_candidates[len(ROLE_LOCAL_PARTS):]} == {"last.first"}
first_last_candidates = generate_candidates("basf.com", "DE", 1_000, requested_pattern="first.last")
assert {candidate.pattern for candidate in first_last_candidates[len(ROLE_LOCAL_PARTS):]} == {"first.last"}
chinese_candidates = generate_candidates("example.cn", "CN", 1_000, requested_pattern="first.last")
chinese_given_names = [
    candidate.email.split("@", 1)[0].split(".", 1)[0]
    for candidate in chinese_candidates[len(ROLE_LOCAL_PARTS):]
]
chinese_profile = COUNTRY_PROFILES["CN"]
single_character_count = sum(name in chinese_profile.given_names for name in chinese_given_names)
compound_character_count = sum(name in chinese_profile.compound_given_names for name in chinese_given_names)
assert single_character_count + compound_character_count == len(chinese_given_names)
assert abs(single_character_count - compound_character_count) <= 1
spanish_candidates = generate_candidates("example.es", "ES", 1_000, requested_pattern="first.last")
spanish_surnames = [
    candidate.email.split("@", 1)[0].split(".", 1)[1]
    for candidate in spanish_candidates[len(ROLE_LOCAL_PARTS):]
]
spanish_profile = COUNTRY_PROFILES["ES"]
single_surname_count = sum(name in spanish_profile.surnames for name in spanish_surnames)
compound_surnames = {name.replace(" ", "") for name in spanish_profile.compound_surnames}
compound_surname_count = sum(name in compound_surnames for name in spanish_surnames)
assert abs(single_surname_count - compound_surname_count) <= 1
learned_first_last = generate_candidates("basf.com", "DE", 1_000, learned_patterns=("first.last", "firstlast"))
assert {candidate.pattern for candidate in learned_first_last[len(ROLE_LOCAL_PARTS):]} == {"first.last"}
german_pair_count = len(COUNTRY_PROFILES["DE"].given_names) * len(COUNTRY_PROFILES["DE"].surnames)
assert german_pair_count == 4_800
after_first_last = generate_candidates(
    "basf.com", "DE", len(ROLE_LOCAL_PARTS) + german_pair_count + 1, requested_pattern="first.last"
)
assert {candidate.pattern for candidate in after_first_last[len(ROLE_LOCAL_PARTS):]} == {"first.last"}

# A derived catalogue provides more combinations without materializing them,
# and an evidence-backed convention must still never fall through to another.
name_catalog = temp_dir / "name_catalog.db"
with sqlite3.connect(name_catalog) as connection:
    connection.execute("CREATE TABLE name_entries (country TEXT, kind TEXT, romanized TEXT, gender TEXT, weight INTEGER)")
    connection.executemany(
        "INSERT INTO name_entries VALUES (?, ?, ?, ?, ?)",
        [("DE", "given", f"given{index}", "U", 1) for index in range(100)]
        + [("DE", "surname", f"surname{index}", "U", 1) for index in range(100)],
    )
previous_catalogue_path = settings.name_catalog_path
object.__setattr__(settings, "name_catalog_path", name_catalog)
derived = generate_candidates("basf.com", "DE", 1_000, requested_pattern="first.last")
object.__setattr__(settings, "name_catalog_path", previous_catalogue_path)
assert len(derived) == 1_000
assert {candidate.pattern for candidate in derived[len(ROLE_LOCAL_PARTS):]} == {"first.last"}
assert {candidate.source for candidate in derived[len(ROLE_LOCAL_PARTS):]} == {"user_selected_pattern"}

# CN uses frequency ordering within each name length, while alternating
# two-character and three-character full-name shapes exactly.
with sqlite3.connect(name_catalog) as connection:
    connection.execute("DROP TABLE name_entries")
    connection.execute(
        "CREATE TABLE name_entries (country TEXT, kind TEXT, romanized TEXT, gender TEXT, name_characters INTEGER, weight INTEGER)"
    )
    connection.executemany(
        "INSERT INTO name_entries VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("CN", "given", "wei", "U", 1, 100),
            ("CN", "given", "ming", "U", 1, 90),
            ("CN", "given", "zihao", "U", 2, 100),
            ("CN", "given", "yuchen", "U", 2, 90),
            ("CN", "surname", "wang", "U", 0, 100),
            ("CN", "surname", "li", "U", 0, 90),
        ],
    )
object.__setattr__(settings, "name_catalog_path", name_catalog)
cn_ranked = generate_candidates("example.cn", "CN", len(ROLE_LOCAL_PARTS) + 8, requested_pattern="first.last")
object.__setattr__(settings, "name_catalog_path", previous_catalogue_path)
assert [item.email.split("@", 1)[0] for item in cn_ranked[len(ROLE_LOCAL_PARTS):]] == [
    "wei.wang", "zihao.wang", "wei.li", "zihao.li", "ming.wang", "yuchen.wang", "ming.li", "yuchen.li",
]
assert infer_email_pattern("example.com", "John", "Smith", "smith.john@example.com") == "last.first"
try:
    normalize_company_domain("gmail.com")
except ValueError:
    pass
else:
    raise AssertionError("Public mailbox domains must be rejected")


def verified_session(email: str) -> str:
    user = auth_store.create_user(email, "smoke-password")
    code = auth_store.create_email_verification(user.id)
    verified = auth_store.confirm_email_verification(user.id, code)
    assert verified is not None and verified.email_verified
    return auth_store.create_session(user.id)


with TestClient(app) as client:
    outsider_session = verified_session("outside@example.com")
    client.cookies.set("verigo_session", outsider_session)
    assert client.get("/prospecting-beta").status_code == 403
    assert client.post("/api/prospecting-beta/runs", json={"domain": "example.com"}).status_code == 403

    beta_session = verified_session("4671793@qq.com")
    client.cookies.set("verigo_session", beta_session)
    page = client.get("/prospecting-beta")
    assert page.status_code == 200
    assert page.headers["x-robots-tag"] == "noindex, nofollow, noarchive"

    missing_country = client.post("/api/prospecting-beta/runs", json={"domain": "example.com"})
    assert missing_country.status_code == 422
    created = client.post("/api/prospecting-beta/runs", json={
        "domain": "example.com", "country": "DE", "email_pattern": "last.first",
    })
    assert created.status_code == 202, created.text
    run = created.json()
    assert run["total"] == 128
    assert run["country"] == "DE"
    assert run["requested_pattern"] == "last.first"
    assert run["summary"]["verified"] == 0

    candidates = prospecting_store.candidates(run["id"])
    assert candidates[0]["category"] == "business_entry"
    personal = next(item for item in candidates if item["category"] == "personal_candidate")
    assert personal["pattern"] == "last.first"
    assert personal["source"] == "user_selected_pattern"
    candidate_page = client.get(f"/api/prospecting-beta/runs/{run['id']}/candidates?limit=2")
    assert candidate_page.status_code == 200, candidate_page.text
    assert candidate_page.json()["total"] == 128
    assert len(candidate_page.json()["items"]) == 2
    assert candidate_page.json()["items"][0]["email"] == candidates[0]["email"]
    job = job_store.get(run["verification_job_id"])
    assert job is not None
    assert job.worker_count == settings.max_workers_per_job
    for result in job.results:
        result["deliverable"] = False
        result["valid"] = False
        result["domain_type"] = "normal"
        result["progress_state"] = "completed"
    job.results[0].update({
        "deliverable": True, "valid": True,
        "smtp_result": "250 accepted", "smtp_raw_result": "250 accepted",
        "checks": {"smtp": True},
    })
    job.results[1].update({"deliverable": True, "valid": True, "domain_type": "catch-all"})
    job.results[personal["original_index"]].update({"deliverable": True, "valid": True})
    job.status = "completed"
    job.finished_at = utc_now()
    job_store.persist(job)
    prospecting_store.finalize_run(job.id, job.results)

    completed = client.get(f"/api/prospecting-beta/runs/{run['id']}")
    assert completed.status_code == 200, completed.text
    payload = completed.json()
    assert payload["summary"]["verified"] == 2
    assert payload["summary"]["catch_all"] == 1
    assert payload["result_total"] == 3
    page = client.get(f"/api/prospecting-beta/runs/{run['id']}/results?limit=2")
    assert page.status_code == 200, page.text
    assert page.json()["total"] == 3
    assert len(page.json()["items"]) == 2
    stored_run = prospecting_store.get_by_job_id(run["verification_job_id"])
    assert stored_run is not None
    assert prospecting_store.domain_patterns(stored_run.owner_id, "example.com") == [personal["pattern"]]
    saved = client.get("/api/prospecting-beta/saved-contacts")
    assert saved.status_code == 200, saved.text
    assert saved.json()["workspace_total"] == 2
    assert saved.json()["total"] == 2
    assert saved.json()["domains"][0]["domain"] == "example.com"
    assert saved.json()["domains"][0]["contact_count"] == 2
    assert saved.json()["domain_total"] == 1
    assert all(item["last_verified_at"] and item["confidence"] >= 90 for item in saved.json()["items"])
    assert {item["email"] for item in saved.json()["items"]} == {
        candidates[0]["email"], personal["email"],
    }
    assert prospecting_store.control_sample_for_job(run["verification_job_id"]) == candidates[0]["email"]
    first_page = client.get("/api/prospecting-beta/saved-contacts?domain=example.com&limit=1")
    assert first_page.status_code == 200, first_page.text
    assert first_page.json()["total"] == 2
    assert len(first_page.json()["items"]) == 1
    contact_email = first_page.json()["items"][0]["email"]
    updated_contact = client.patch("/api/prospecting-beta/saved-contacts", json={
        "email": contact_email, "favorite": True, "tags": ["follow-up", "de"]
    })
    assert updated_contact.status_code == 200, updated_contact.text
    assert updated_contact.json()["favorite"] is True
    assert updated_contact.json()["tags"] == ["follow-up", "de"]
    company = client.get("/api/prospecting-beta/companies/example.com")
    assert company.status_code == 200, company.text
    assert company.json()["contact_count"] == 2
    exported = client.get("/api/prospecting-beta/saved-contacts/export?domain=example.com")
    assert exported.status_code == 200 and "text/csv" in exported.headers["content-type"]

    # Another verified buyer only receives shared confirmations after explicitly
    # creating a discovery request for this exact company; they cannot browse
    # the first buyer's saved-contact workspace.
    object.__setattr__(settings, "prospecting_beta_allowed_emails", frozenset({
        "4671793@qq.com", "buyer@example.net",
    }))
    buyer_session = verified_session("buyer@example.net")
    client.cookies.set("verigo_session", buyer_session)
    second = client.post("/api/prospecting-beta/runs", json={
        "domain": "example.com", "country": "DE",
        "known_first_name": "John", "known_last_name": "Smith",
        "known_email": "smith.john@example.com",
    })
    assert second.status_code == 202, second.text
    second_run = second.json()
    assert second_run["requested_pattern"] == "last.first"
    shared_results = client.get(f"/api/prospecting-beta/runs/{second_run['id']}/results")
    assert shared_results.status_code == 200, shared_results.text
    assert {item["email"] for item in shared_results.json()["items"]} >= {
        candidates[0]["email"], personal["email"],
    }
    assert all(item["result_type"] == "verified" for item in shared_results.json()["items"])
    assert all(item["verification"]["smtp_result"] == "250 accepted" for item in shared_results.json()["items"])
    buyer_contacts = client.get("/api/prospecting-beta/saved-contacts")
    assert buyer_contacts.status_code == 200 and buyer_contacts.json()["total"] == 0
    second_candidates = prospecting_store.candidates(second_run["id"])
    assert {item["email"] for item in candidates}.isdisjoint(
        {item["email"] for item in second_candidates}
    )
    with sqlite3.connect(settings.database_path) as connection:
        first_name_keys = {
            row[0] for row in connection.execute(
                "SELECT name_key FROM prospecting_candidates WHERE run_id=? AND name_key IS NOT NULL",
                (run["id"],),
            )
        }
        second_name_keys = {
            row[0] for row in connection.execute(
                "SELECT name_key FROM prospecting_candidates WHERE run_id=? AND name_key IS NOT NULL",
                (second_run["id"],),
            )
        }
    assert first_name_keys.isdisjoint(second_name_keys)
    assert {item["pattern"] for item in second_candidates if item["category"] == "personal_candidate"} == {"last.first"}

    # Continue the receiver-protection case in the original owner's workspace.
    client.cookies.set("verigo_session", beta_session)

    # The configured catalogue is intentionally one batch. Existing candidates
    # must expand it so a later run still reserves a fresh batch.
    third = client.post("/api/prospecting-beta/runs", json={
        "domain": "example.com", "country": "DE", "email_pattern": "last.first",
    })
    assert third.status_code == 202, third.text
    third_candidates = prospecting_store.candidates(third.json()["id"])
    assert len(third_candidates) == 128
    assert {item["email"] for item in second_candidates}.isdisjoint(
        {item["email"] for item in third_candidates}
    )

    stopped = client.post(f"/api/prospecting-beta/runs/{third.json()['id']}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "stopped"
    stopped_read = client.get(f"/api/prospecting-beta/runs/{third.json()['id']}")
    assert stopped_read.status_code == 200
    assert stopped_read.json()["status"] == "stopped"

    protection = prospecting_store.apply_protection_outcomes(third.json()["verification_job_id"], [{
        "original_index": 0,
        "smtp_result": "550 5.7.1 anti-enumeration policy rejected recipient discovery",
    }])
    assert protection is None
    assert prospecting_store.protection_status("example.com")["state"] == "clear"
    protection = prospecting_store.apply_protection_outcomes(
        third.json()["verification_job_id"], [{
            "original_index": 0,
            "smtp_result": "550 5.7.1 anti-enumeration policy rejected recipient discovery",
        }], control_probes=[{
            "email": candidates[0]["email"],
            "result": {"smtp_result": "550", "smtp_raw_result": "550", "deliverable": False},
        }],
    )
    assert protection is not None and protection["action"] == "stop"
    assert prospecting_store.protection_status("example.com")["state"] == "stopped"
    protected = client.post("/api/prospecting-beta/runs", json={
        "domain": "example.com", "country": "DE", "email_pattern": "last.first",
    })
    assert protected.status_code == 429

    generic = client.post("/api/prospecting-beta/runs", json={
        "domain": "generic550.example", "country": "DE", "email_pattern": "last.first",
    })
    assert generic.status_code == 202, generic.text
    generic_policy = prospecting_store.apply_protection_outcomes(
        generic.json()["verification_job_id"],
        [{"original_index": index, "smtp_result": "550", "deliverable": False} for index in range(6)]
        + [{"original_index": 7, "smtp_result": "450 rate limited", "deliverable": False}],
    )
    assert generic_policy is None
    assert prospecting_store.protection_status("generic550.example")["state"] == "clear"

    normalized_550 = normalize_result({
        "smtp_result": "550 recipient verification is not permitted",
        "deliverable": False,
        "valid": False,
    })
    assert normalized_550["smtp_code"] == "550"
    assert normalized_550["smtp_raw_result"] == "550 recipient verification is not permitted"

    company_file = "Company Name,Website,Country,Industry\nAcme GmbH,https://www.acme.example,DE,Industrial\nNo Site,,DE,Industrial\n"
    imported_companies = client.post(
        "/api/prospecting-beta/companies/import",
        files={"file": ("companies.csv", company_file.encode(), "text/csv")},
    )
    assert imported_companies.status_code == 200, imported_companies.text
    assert imported_companies.json()["imported"] == 2
    companies = client.get("/api/prospecting-beta/companies?domain_state=ready")
    assert companies.status_code == 200, companies.text
    assert companies.json()["total"] == 1
    company = companies.json()["items"][0]
    selected_company = client.patch(
        f"/api/prospecting-beta/companies/{company['id']}", json={"selected": True}
    )
    assert selected_company.status_code == 200, selected_company.text
    discovered_company = client.post("/api/prospecting-beta/companies/discover", json={
        "company_ids": [company["id"]], "country": "DE",
    })
    assert discovered_company.status_code == 202, discovered_company.text
    assert len(discovered_company.json()["runs"]) == 1
    company_run = discovered_company.json()["runs"][0]
    company_stored_run = prospecting_store.get(company_run["run_id"], stored_run.owner_id)
    assert company_stored_run is not None
    company_job = job_store.get(company_stored_run.verification_job_id)
    assert company_job is not None and len(company_job.emails) == 1

print("prospecting beta smoke: ok")
