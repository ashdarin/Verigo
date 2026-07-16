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

from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.core.legacy import load_legacy_module
from app.core.security import token_hash
from app.db.jobs import Job, job_store
from app.main import app
from app.tasks.verification import verification_tasks


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

    original_submit = verification_tasks._executor.submit
    verification_tasks._executor.submit = lambda *_args, **_kwargs: None
    try:
        created = guest.post(
            "/api/jobs",
            json={"emails": ["api-check@example.com"], "worker_count": 1},
        )
    finally:
        verification_tasks._executor.submit = original_submit
    assert created.status_code == 202, created.text
    created_body = created.json()
    assert created_body["access_token"]
    assert guest.get(f"/api/jobs/{created_body['id']}").status_code == 404
    assert guest.get(
        f"/api/jobs/{created_body['id']}",
        headers={"X-Job-Token": created_body["access_token"]},
    ).status_code == 200


with TestClient(app) as account:
    registered = account.post(
        "/api/auth/register",
        json={"username": "smoke_user", "password": "correct-horse-2026"},
    )
    assert registered.status_code == 201, registered.text
    user_id = registered.json()["id"]
    assert account.get("/api/auth/me").json()["username"] == "smoke_user"

    job_store.add(completed_job("ownedjob0001", owner_id=user_id))
    jobs = account.get("/api/jobs")
    assert jobs.status_code == 200
    assert [job["id"] for job in jobs.json()] == ["ownedjob0001"]
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
