from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


temp_dir = Path(tempfile.mkdtemp(prefix="verigo-prospecting-beta-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
os.environ["VERIGO_DATABASE_PATH"] = str(temp_dir / "verigo.db")
os.environ["VERIGO_RESULTS_DIR"] = str(temp_dir / "results")
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
assert {candidate.pattern for candidate in after_first_last[len(ROLE_LOCAL_PARTS):-1]} == {"first.last"}
assert after_first_last[-1].pattern == "f.last"
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
    job = job_store.get(run["verification_job_id"])
    assert job is not None
    assert job.worker_count == settings.max_workers_per_job
    for result in job.results:
        result["deliverable"] = False
        result["valid"] = False
        result["domain_type"] = "normal"
        result["progress_state"] = "completed"
    job.results[0].update({"deliverable": True, "valid": True})
    job.results[1].update({"deliverable": True, "valid": True, "domain_type": "catch-all"})
    job.results[personal["original_index"]].update({"deliverable": True, "valid": True})
    job.status = "completed"
    job.finished_at = utc_now()
    job_store.persist(job)

    completed = client.get(f"/api/prospecting-beta/runs/{run['id']}")
    assert completed.status_code == 200, completed.text
    payload = completed.json()
    assert payload["summary"]["verified"] == 2
    assert payload["summary"]["catch_all"] == 1
    assert any(item["result_type"] == "catch_all" for item in payload["results"])
    assert prospecting_store.domain_patterns("example.com") == [personal["pattern"]]
    saved = client.get("/api/prospecting-beta/saved-contacts")
    assert saved.status_code == 200, saved.text
    assert saved.json()["total"] == 2
    assert saved.json()["domains"][0]["domain"] == "example.com"
    assert saved.json()["domains"][0]["contact_count"] == 2
    assert {item["email"] for item in saved.json()["items"]} == {
        candidates[0]["email"], personal["email"],
    }
    first_page = client.get("/api/prospecting-beta/saved-contacts?domain=example.com&limit=1")
    assert first_page.status_code == 200, first_page.text
    assert first_page.json()["total"] == 2
    assert len(first_page.json()["items"]) == 1

    second = client.post("/api/prospecting-beta/runs", json={
        "domain": "example.com", "country": "DE",
        "known_first_name": "John", "known_last_name": "Smith",
        "known_email": "smith.john@example.com",
    })
    assert second.status_code == 202, second.text
    second_run = second.json()
    assert second_run["requested_pattern"] == "last.first"
    second_candidates = prospecting_store.candidates(second_run["id"])
    assert {item["email"] for item in candidates}.isdisjoint(
        {item["email"] for item in second_candidates}
    )
    assert {item["pattern"] for item in second_candidates if item["category"] == "personal_candidate"} == {"last.first"}

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
    assert protection is not None and protection["action"] == "stop"
    assert prospecting_store.protection_status("example.com")["state"] == "stopped"
    protected = client.post("/api/prospecting-beta/runs", json={
        "domain": "example.com", "country": "DE", "email_pattern": "last.first",
    })
    assert protected.status_code == 429

print("prospecting beta smoke: ok")
