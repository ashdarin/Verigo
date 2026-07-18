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
os.environ["VERIGO_TRIAL_NETWORK_LIMIT"] = "2"
os.environ["VERIGO_ADMIN_EMAILS"] = "admin@example.com"
os.environ["VERIGO_METRICS_SALT"] = "smoke-test-metrics-salt"
os.environ["VERIGO_TENCENT_QQ_WORKER_ENABLED"] = "true"
os.environ["VERIGO_TENCENT_QQ_WORKER_TOKEN"] = "smoke-tencent-worker-token"

from fastapi.testclient import TestClient
from openpyxl import Workbook

import app.api.auth as auth_api
from app.core.legacy import load_legacy_module
from app.core.security import hash_password, token_hash
from app.db.auth import auth_store
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
    assert guest.get("/dashboard").status_code == 200
    assert guest.get("/").status_code == 200
    robots = guest.get("/robots.txt")
    assert robots.status_code == 200 and "Sitemap: https://verigo.site/sitemap.xml" in robots.text
    sitemap = guest.get("/sitemap.xml")
    assert sitemap.status_code == 200 and "https://verigo.site/privacy" in sitemap.text
    assert guest.get("/privacy").status_code == 200
    assert guest.get("/acceptable-use").status_code == 200
    assert guest.get("/email-verification").status_code == 200
    assert guest.get("/bulk-email-verification").status_code == 200
    assert guest.get("/email-list-cleaning").status_code == 200
    assert guest.get("/api/admin/metrics").status_code == 401
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
    guest_single = guest.post(
        "/api/verify/single", json={"email": "api-check@example.com"}
    )
    assert guest_single.status_code == 202, guest_single.text
    stopped_guest_job = guest.post(
        f"/api/jobs/{guest_single.json()['id']}/stop",
        headers={"X-Job-Token": guest_single.json()["access_token"]},
    )
    assert stopped_guest_job.status_code == 200, stopped_guest_job.text
    assert stopped_guest_job.json()["status"] == "stopped"

    worker_headers = {
        "X-Verigo-Worker-Token": "smoke-tencent-worker-token",
        "X-Verigo-Worker-Id": "smoke-cloudstudio",
    }
    assert guest.post("/api/workers/tencent-qq/claim").status_code == 401
    remote_stopped = verification_tasks.submit(
        ["worker-stop@qq.com"], worker_count=1, execution_target="tencent_qq"
    )
    claimed_stopped = guest.post("/api/workers/tencent-qq/claim", headers=worker_headers)
    assert claimed_stopped.status_code == 200, claimed_stopped.text
    assert claimed_stopped.json()["job"]["id"] == remote_stopped.id
    stopped_remote = guest.post(
        f"/api/jobs/{remote_stopped.id}/stop",
        headers={"X-Job-Token": remote_stopped.guest_token},
    )
    assert stopped_remote.status_code == 200, stopped_remote.text
    heartbeat = guest.post(
        f"/api/workers/tencent-qq/jobs/{remote_stopped.id}/heartbeat", headers=worker_headers
    )
    assert heartbeat.json()["stop_requested"] is True

    remote_completed = verification_tasks.submit(
        ["worker-complete@qq.com"], worker_count=1, execution_target="tencent_qq"
    )
    claimed_completed = guest.post("/api/workers/tencent-qq/claim", headers=worker_headers)
    assert claimed_completed.json()["job"]["id"] == remote_completed.id
    worker_result = {
        "email": "worker-complete@qq.com", "original_index": 0,
        "deliverable": True, "valid": True, "verification_method": "qq_optimized",
    }
    reported = guest.post(
        f"/api/workers/tencent-qq/jobs/{remote_completed.id}/results",
        headers=worker_headers, json={"results": [worker_result]},
    )
    assert reported.status_code == 200, reported.text
    completed_remote = guest.post(
        f"/api/workers/tencent-qq/jobs/{remote_completed.id}/complete",
        headers=worker_headers, json={"results": [worker_result]},
    )
    assert completed_remote.status_code == 200, completed_remote.text
    assert completed_remote.json()["status"] == "completed"


with TestClient(app) as account:
    registered = account.post(
        "/api/auth/register",
        json={"email": "smoke@example.com", "password": "correct-horse-2026"},
    )
    assert registered.status_code == 201, registered.text
    user_id = registered.json()["id"]
    assert account.get("/api/auth/me").json()["email"] == "smoke@example.com"
    assert account.get("/api/admin/metrics").status_code == 403
    assert account.post(
        "/api/auth/password/change",
        json={"current_password": "incorrect-password", "new_password": "new-password-2026"},
    ).status_code == 422
    changed = account.post(
        "/api/auth/password/change",
        json={"current_password": "correct-horse-2026", "new_password": "new-password-2026"},
    )
    assert changed.status_code == 204, changed.text
    assert account.get("/api/auth/me").json()["id"] == user_id
    assert account.post("/api/auth/logout").status_code == 204
    assert account.post(
        "/api/auth/login",
        json={"account": "smoke@example.com", "password": "correct-horse-2026"},
    ).status_code == 401
    assert account.post(
        "/api/auth/login",
        json={"account": "smoke@example.com", "password": "new-password-2026"},
    ).status_code == 200
    assert account.post(
        "/api/auth/register",
        json={"email": "blocked@mailinator.com", "password": "correct-horse-2026"},
    ).status_code == 422
    assert account.post(
        "/api/verify/single", json={"email": "first@example.com"}
    ).status_code == 202

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
    discovery_job = account.post(
        "/api/discovery/verify",
        json={"first_name": "Ming", "last_name": "Wang", "domain": "example.com"},
    )
    assert discovery_job.status_code == 202, discovery_job.text
    assert discovery_job.json()["stop_on_deliverable"] is True
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
    third_free = account.post(
        "/api/verify/single", json={"email": "third@example.com"}
    )
    assert third_free.status_code == 202, third_free.text

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
    stopped_paid = account.post(f"/api/jobs/{paid.json()['id']}/stop")
    assert stopped_paid.status_code == 200, stopped_paid.text
    assert stopped_paid.json()["status"] == "stopped"
    stale_worker_copy = job_store.get(paid.json()["id"])
    assert stale_worker_copy is not None
    stale_worker_copy.status = "completed"
    job_store.persist(stale_worker_copy)
    assert job_store.get(paid.json()["id"]).status == "stopped"
    auth_store.refund_credits(user_id, 2, f"verification:{paid.json()['id']}")
    assert account.get("/api/auth/me").json()["credits"] == 10

    job_store.add(completed_job("ownedjob0001", owner_id=user_id))
    jobs = account.get("/api/jobs")
    assert jobs.status_code == 200
    assert "ownedjob0001" in [job["id"] for job in jobs.json()]
    assert account.get("/api/jobs/ownedjob0001").status_code == 200

    assert account.post("/api/auth/logout").status_code == 204
    assert account.get("/api/jobs/ownedjob0001").status_code == 404


with TestClient(app) as admin_account:
    registered = admin_account.post(
        "/api/auth/register",
        json={"email": "admin@example.com", "password": "correct-horse-2026"},
    )
    assert registered.status_code == 201, registered.text
    assert registered.json()["is_admin"] is False
    admin_id = registered.json()["id"]
    verification_code = auth_store.create_email_verification(admin_id)
    auth_store.confirm_email_verification(admin_id, verification_code)
    admin_user = admin_account.get("/api/auth/me").json()
    assert admin_user["is_admin"] is True
    admin_credits = admin_user["credits"]
    admin_job = admin_account.post(
        "/api/jobs",
        json={
            "emails": [f"admin-check-{number}@example.com" for number in range(admin_credits + 1)],
            "worker_count": 1,
        },
    )
    assert admin_job.status_code == 202, admin_job.text
    assert admin_account.get("/api/auth/me").json()["credits"] == admin_credits
    with auth_store._connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM credit_ledger WHERE user_id=? AND reference=?",
            (admin_id, f"verification:{admin_job.json()['id']}"),
        ).fetchone() is None
    metrics = admin_account.get("/api/admin/metrics")
    assert metrics.status_code == 200, metrics.text
    assert metrics.json()["today"]["page_views"] >= 1
    assert len(metrics.json()["daily"]) == 14


for number in range(3):
    network_user = auth_store.create_user(
        f"network-{number}@example.com", "correct-horse-2026"
    )
    network_code = auth_store.create_email_verification(network_user.id)
    network_verified = auth_store.confirm_email_verification(
        network_user.id, network_code, network_hash="shared-network-test"
    )
    assert network_verified.email_verified is True
    assert network_verified.trial_credits == (10 if number < 2 else 0)


legacy_id = "legacy-smoke-user"
with auth_store._connect() as connection:
    connection.execute(
        """
        INSERT INTO users(id, username, email, email_verified, credits, password_hash, created_at)
        VALUES (?, ?, NULL, 0, 7, ?, '2026-01-01T00:00:00+00:00')
        """,
        (legacy_id, "legacy_user", hash_password("legacy-password")),
    )

with TestClient(app) as legacy_account:
    legacy_login = legacy_account.post(
        "/api/auth/login",
        json={"account": "legacy_user", "password": "legacy-password"},
    )
    assert legacy_login.status_code == 200, legacy_login.text
    assert legacy_login.json()["needs_email_binding"] is True
    assert legacy_login.json()["credits"] == 7

    legacy_login_compatibility = legacy_account.post(
        "/api/auth/login",
        json={"email": "legacy_user", "password": "legacy-password"},
    )
    assert legacy_login_compatibility.status_code == 200, legacy_login_compatibility.text

    original_send_email_binding = auth_api.send_email_binding
    auth_api.send_email_binding = lambda *_args, **_kwargs: None
    try:
        binding_request = legacy_account.post(
            "/api/auth/email-binding/request", json={"email": "legacy@example.com"}
        )
    finally:
        auth_api.send_email_binding = original_send_email_binding
    assert binding_request.status_code == 204, binding_request.text

    binding_code = auth_store.create_email_binding(legacy_id, "legacy@example.com")
    bound = legacy_account.post(
        "/api/auth/email-binding/confirm", json={"code": binding_code}
    )
    assert bound.status_code == 200, bound.text
    assert bound.json()["email"] == "legacy@example.com"
    assert bound.json()["email_verified"] is True
    assert bound.json()["needs_email_binding"] is False
    assert bound.json()["credits"] == 7
    assert bound.json()["trial_credits"] == 0

    assert legacy_account.post("/api/auth/logout").status_code == 204
    rebound_login = legacy_account.post(
        "/api/auth/login",
        json={"account": "legacy@example.com", "password": "legacy-password"},
    )
    assert rebound_login.status_code == 200, rebound_login.text


with TestClient(app) as deletion_account:
    registered = deletion_account.post(
        "/api/auth/register",
        json={"email": "delete-me@example.com", "password": "correct-horse-2026"},
    )
    assert registered.status_code == 201, registered.text
    assert deletion_account.delete("/api/auth/account").status_code == 204
    assert deletion_account.get("/api/auth/me").json() is None


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
