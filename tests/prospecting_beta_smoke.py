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

from fastapi.testclient import TestClient

from app.config import settings
from app.core.prospecting import ROLE_LOCAL_PARTS, generate_candidates, infer_email_pattern, normalize_company_domain
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
    assert {item["email"] for item in saved.json()["items"]} == {
        candidates[0]["email"], personal["email"],
    }

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

    stopped = client.post(f"/api/prospecting-beta/runs/{run['id']}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "completed"

print("prospecting beta smoke: ok")
