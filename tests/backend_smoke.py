from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path


temp_dir = Path(tempfile.mkdtemp(prefix="verigo-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
os.environ["VERIGO_DATABASE_PATH"] = str(temp_dir / "verigo.db")
os.environ["VERIGO_RESULTS_DIR"] = str(temp_dir / "results")
os.environ["VERIGO_SECURE_COOKIES"] = "false"
os.environ["VERIGO_FREE_SINGLE_DAILY_LIMIT"] = "2"
os.environ["VERIGO_EMAIL_VERIFICATION_TRIAL_CREDITS"] = "10"
os.environ["VERIGO_TRIAL_CREDIT_DAYS"] = "7"
os.environ["VERIGO_MAX_PENDING_JOBS"] = "50"

from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.core.legacy import load_legacy_module
from app.core.security import token_hash
from app.db.auth import auth_store
from app.db.jobs import Job, job_store
from app.main import app


def completed_job(job_id: str, **kwargs) -> Job:
    return Job(
        id=job_id,
        emails=["check@example.com"],
        worker_count=1,
        status="completed",
        results=[{"email": "check@example.com", "deliverable": True}],
        **kwargs,
    )


with TestClient(app) as guest:
    assert guest.get("/api/health").json() == {"status": "ok"}
    assert guest.get("/api/jobs").status_code == 401

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["name", "email"])
    sheet.append(["A", "first@example.com"])
    sheet.append(["B", "text with second@example.cn inside"])
    payload = io.BytesIO()
    workbook.save(payload)
    imported = guest.post(
        "/api/import",
        files={"file": ("contacts.xlsx", payload.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["emails"] == ["first@example.com", "second@example.cn"]

    guest_token = "guest-test-token"
    job_store.add(
        completed_job(
            "guestjob0001",
            guest_token_hash=token_hash(guest_token),
        )
    )
    assert guest.get("/api/jobs/guestjob0001").status_code == 404
    assert guest.get(
        "/api/jobs/guestjob0001", headers={"X-Job-Token": guest_token}
    ).status_code == 200
    live_results = guest.get(
        "/api/jobs/guestjob0001/results?limit=50",
        headers={"X-Job-Token": guest_token},
    )
    assert live_results.status_code == 200
    assert live_results.json()["total"] == 1
    assert live_results.json()["available"] == 1

    assert guest.post(
        "/api/jobs",
        json={"emails": ["api-check@example.com"], "worker_count": 1},
    ).status_code == 401
    assert guest.post(
        "/api/verify/single", json={"email": "api-check@example.com"}
    ).status_code == 401


with TestClient(app) as account:
    registered = account.post(
        "/api/auth/register",
        json={"email": "smoke@example.com", "password": "correct-horse-2026"},
    )
    assert registered.status_code == 201, registered.text
    user_id = registered.json()["id"]
    assert account.get("/api/auth/me").json()["email"] == "smoke@example.com"
    assert account.post(
        "/api/verify/single", json={"email": "first@example.com"}
    ).status_code == 403

    verification_code = auth_store.create_email_verification(user_id)
    auth_store.confirm_email_verification(user_id, verification_code)
    verified_user = account.get("/api/auth/me").json()
    assert verified_user["email_verified"] is True
    assert verified_user["credits"] == 10
    assert verified_user["paid_credits"] == 0
    assert verified_user["trial_credits"] == 10
    assert verified_user["trial_credit_expires_at"]

    candidates = account.post(
        "/api/discovery/candidates",
        json={"first_name": "Ming", "last_name": "Wang", "domain": "example.com"},
    )
    assert candidates.status_code == 200, candidates.text
    assert candidates.json()["candidates"]
    assert account.get("/api/auth/me").json()["credits"] == 10

    first_free = account.post(
        "/api/verify/single", json={"email": "first@example.com"}
    )
    second_free = account.post(
        "/api/verify/single", json={"email": "second@example.com"}
    )
    assert first_free.status_code == 202, first_free.text
    assert second_free.status_code == 202, second_free.text
    assert account.get("/api/auth/me").json()["credits"] == 10
    exhausted = account.post(
        "/api/verify/single", json={"email": "third@example.com"}
    )
    assert exhausted.status_code == 429, exhausted.text

    paid = account.post(
        "/api/jobs",
        json={
            "emails": ["paid-one@example.com", "paid-two@example.com"],
            "worker_count": 2,
        },
    )
    assert paid.status_code == 202, paid.text
    after_paid = account.get("/api/auth/me").json()
    assert after_paid["credits"] == 8
    assert after_paid["paid_credits"] == 0
    assert after_paid["trial_credits"] == 8
    auth_store.refund_credits(user_id, 2, f"verification:{paid.json()['id']}")
    assert account.get("/api/auth/me").json()["credits"] == 10

    job_store.add(completed_job("ownedjob0001", owner_id=user_id))
    jobs = account.get("/api/jobs")
    assert jobs.status_code == 200
    assert "ownedjob0001" in [job["id"] for job in jobs.json()]
    assert account.get("/api/jobs/ownedjob0001").status_code == 200

    assert account.post("/api/auth/logout").status_code == 204
    assert account.get("/api/jobs/ownedjob0001").status_code == 404


legacy = load_legacy_module()
verifier = legacy.EmailVerifier()
config = verifier.get_consumer_fix_strategy("qq.com")
assert config["use_data_command"] is False
assert verifier._handle_qq_response(250, b"OK", config, 0)[0] is True
assert verifier._handle_qq_response(550, b"Mailbox not found", config, 0)[0] is False
assert verifier._handle_qq_response(550, b"Access denied by policy", config, config["max_attempts"] - 1)[0] is False

missing_domain = legacy.EmailVerifier()
missing_domain.check_domain_exists = lambda _domain: False
missing = missing_domain.verify_email_comprehensive("person@missing-domain.test")
assert missing["deliverable"] is False
assert missing["checks"]["smtp"] is False

missing_mx = legacy.EmailVerifier()
missing_mx.check_domain_exists = lambda _domain: True
missing_mx.get_mx_records = lambda _domain: []
no_mx = missing_mx.verify_email_comprehensive("person@no-mx.test")
assert no_mx["deliverable"] is False
assert no_mx["checks"]["smtp"] is False

class ClosedConnectionSMTP:
    def __init__(self, *args, **kwargs):
        pass

    def connect(self, *args, **kwargs):
        raise legacy.smtplib.SMTPServerDisconnected("connection unexpectedly closed")

    def quit(self):
        pass


original_smtp = legacy.smtplib.SMTP
legacy.smtplib.SMTP = ClosedConnectionSMTP
try:
    closed_config = dict(config, max_attempts=1, mx_delay=0)
    closed, closed_detail = verifier.check_smtp_delivery_fixed(
        "person@qq.com", "mx.test", closed_config
    )
finally:
    legacy.smtplib.SMTP = original_smtp
assert closed is False
assert "SMTP连接被服务器关闭" in closed_detail

print("backend smoke: ok")
