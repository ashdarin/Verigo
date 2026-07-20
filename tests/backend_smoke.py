from __future__ import annotations

import io
import os
import sys
import tempfile
from contextlib import nullcontext
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
os.environ["VERIGO_CLOUDSTUDIO_PROBE_TOKEN"] = "smoke-cloudstudio-probe-token"
os.environ["VERIGO_TENCENT_QQ_WORKER_TOKEN"] = "smoke-tencent-worker-token"
os.environ["VERIGO_TENCENT_QQ_WORKER_ENABLED"] = "true"
os.environ["VERIGO_TENCENT_QQ_WORKER_ALLOWED_EMAILS"] = "smoke@example.com"
os.environ["VERIGO_GMAIL_WORKER_TOKEN"] = "smoke-gmail-worker-token"
os.environ["VERIGO_GMAIL_WORKER_ENABLED"] = "true"
os.environ["VERIGO_GMAIL_WORKER_ALLOWED_EMAILS"] = "smoke@example.com"

from fastapi.testclient import TestClient
from openpyxl import Workbook

import app.api.auth as auth_api
from app.api.routes import gmail_target, submit_routed_job, tencent_qq_target
from app.config import settings
from app.core.legacy import load_legacy_module
from app.core.result_retry import (
    is_smtp_greylisted,
    is_temporary_smtp_452,
    smtp_temporary_status,
)
from app.core.security import hash_password, token_hash
from app.db.auth import auth_store
from app.db.jobs import Job, job_store, utc_now
from app.main import app
from app.tasks.verification import (
    normalize_result,
    schedule_deferred_temporary_retry,
    sync_parent_job,
)


def completed_job(job_id: str, **kwargs) -> Job:
    return Job(
        id=job_id,
        emails=["check@example.com"],
        worker_count=1,
        status="completed",
        results=[{"email": "check@example.com", "deliverable": True}],
        **kwargs,
    )


assert tencent_qq_target(["person@qq.com"], "smoke@example.com") == "tencent_qq"
assert tencent_qq_target(["person@qq.com"], "other@example.com") == "local"
assert tencent_qq_target(["person@example.com"], "smoke@example.com") == "local"
assert gmail_target(["person@gmail.com"], "smoke@example.com") == "gmail"
assert gmail_target(["person@gmail.com"], "other@example.com") == "local"
assert gmail_target(["person@example.com"], "smoke@example.com") == "local"
assert is_temporary_smtp_452({"smtp_result": "452 temporary mailbox failure"})
assert is_temporary_smtp_452({"message": "452 暂时无法确认"})
assert not is_temporary_smtp_452({"smtp_result": "550 mailbox unavailable"})
assert smtp_temporary_status({"smtp_result": "421 service not available"}) == "421"
assert smtp_temporary_status({"smtp_result": "450 greylisted"}) == "450"
assert smtp_temporary_status({"smtp_result": "451 local error"}) == "451"
assert smtp_temporary_status({"smtp_result": "550 mailbox unavailable"}) is None
assert is_smtp_greylisted({"smtp_result": "450 4.2.0 Sender address rejected: Greylisted"})

greylisted = normalize_result(
    {
        "email": "pengjie.ai@porsche.cn",
        "valid": False,
        "deliverable": False,
        "checks": {"format": True, "domain": True, "mx": True, "smtp": False},
        "smtp_result": "RCPT TO阶段返回 450: Sender address rejected: Greylisted",
    }
)
assert greylisted["deliverable"] is None
assert greylisted["valid"] is True
assert greylisted["checks"]["smtp"] is None
assert greylisted["temporary_smtp_code"] == "450"
assert "灰名单" in greylisted["smtp_result"]


class TemporarySmtpServer:
    def connect(self, *_args):
        return 220, b"ready"

    def ehlo(self, *_args):
        return 250, b"ok"

    def mail(self, *_args):
        return 250, b"ok"

    def rcpt(self, *_args):
        return 450, b"4.2.0 Sender address rejected: Greylisted"

    def quit(self):
        return None


legacy_module = load_legacy_module()
original_smtp = legacy_module.smtplib.SMTP
original_sleep = legacy_module.time.sleep
try:
    legacy_module.smtplib.SMTP = lambda **_kwargs: TemporarySmtpServer()
    legacy_module.time.sleep = lambda _seconds: None
    verifier = legacy_module.EmailVerifier()
    verifier.smtp_gate = lambda _mx_host: nullcontext(True)
    verifier.record_smtp_response = lambda *_args: None
    verdict, detail = verifier.check_smtp_delivery(
        "pengjie.ai@porsche.cn", "mail.example.test", "fast"
    )
finally:
    legacy_module.smtplib.SMTP = original_smtp
    legacy_module.time.sleep = original_sleep
assert verdict is None
assert "450" in detail

deferred_job = Job(
    id="smoketemp001", emails=["pengjie.ai@porsche.cn"], worker_count=1,
    status="running", results=[greylisted], worker_id="smoke-worker",
)
job_store.add(deferred_job)
assert schedule_deferred_temporary_retry(deferred_job, deferred_job.results)
job_store.persist(deferred_job)
stored_deferred_job = job_store.get(deferred_job.id)
assert stored_deferred_job is not None
assert stored_deferred_job.status == "queued"
assert stored_deferred_job.deferred_retry_at is not None
assert stored_deferred_job.temporary_retry_attempts == 1

object.__setattr__(settings, "tencent_qq_worker_allowed_emails", frozenset({"*"}))
assert tencent_qq_target(["person@qq.com"], "other@example.com") == "tencent_qq"
assert tencent_qq_target(["person@qq.com"], None) == "tencent_qq"
assert tencent_qq_target(["person@example.com"], None) == "local"
object.__setattr__(
    settings, "tencent_qq_worker_allowed_emails", frozenset({"smoke@example.com"})
)

object.__setattr__(settings, "tencent_qq_worker_allowed_emails", frozenset({"*"}))
object.__setattr__(settings, "gmail_worker_allowed_emails", frozenset({"*"}))
yahoo_mixed_parent = submit_routed_job(
    ["skip@yahoo.com", "first@qq.com", "second@gmail.com", "third@example.com"],
    2,
    owner_id="mixed-owner",
    owner_email="mixed-owner@example.com",
    job_id="mixedyahoo001",
)
yahoo_mixed_children = job_store.children(yahoo_mixed_parent.id)
assert {child.execution_target for child in yahoo_mixed_children} == {
    "unsupported", "local", "tencent_qq", "gmail"
}
yahoo_child = next(child for child in yahoo_mixed_children if child.execution_target == "unsupported")
assert yahoo_child.status == "completed"
assert yahoo_child.results[0]["verification_method"] == "不支持验证"
assert yahoo_mixed_parent.results[0]["email"] == "skip@yahoo.com"
for child in yahoo_mixed_children:
    if child.status != "completed":
        job_store.stop(child.id)

mixed_three_way_parent = submit_routed_job(
    ["first@qq.com", "second@gmail.com", "third@example.com", "fourth@googlemail.com"],
    2,
    owner_id="mixed-owner",
    owner_email="mixed-owner@example.com",
    job_id="mixedthree001",
)
mixed_three_way_children = job_store.children(mixed_three_way_parent.id)
assert mixed_three_way_parent.execution_target == "aggregate"
assert {child.execution_target for child in mixed_three_way_children} == {
    "local", "tencent_qq", "gmail"
}
assert next(child for child in mixed_three_way_children if child.execution_target == "tencent_qq").emails == ["first@qq.com"]
assert next(child for child in mixed_three_way_children if child.execution_target == "gmail").emails == ["second@gmail.com", "fourth@googlemail.com"]
assert next(child for child in mixed_three_way_children if child.execution_target == "local").emails == ["third@example.com"]
for child in mixed_three_way_children:
    child.status = "completed"
    child.started_at = utc_now()
    child.finished_at = utc_now()
    child.results = [
        {"email": email, "original_index": index, "deliverable": True}
        for index, email in enumerate(child.emails)
    ]
    job_store.persist(child)
    sync_parent_job(child)
mixed_three_way_parent = job_store.get(mixed_three_way_parent.id)
assert mixed_three_way_parent is not None
assert mixed_three_way_parent.status == "completed"
assert [result["email"] for result in mixed_three_way_parent.results] == [
    "first@qq.com", "second@gmail.com", "third@example.com", "fourth@googlemail.com"
]
object.__setattr__(
    settings, "tencent_qq_worker_allowed_emails", frozenset({"smoke@example.com"})
)
object.__setattr__(
    settings, "gmail_worker_allowed_emails", frozenset({"smoke@example.com"})
)

restart_job = Job(
    id="restartqq001",
    emails=["restart-check@qq.com"],
    worker_count=1,
    execution_target="tencent_qq",
)
job_store.add(restart_job)
job_store._initialized = False
job_store.initialize()
assert job_store.get(restart_job.id).execution_target == "tencent_qq"
job_store.stop(restart_job.id)

object.__setattr__(settings, "tencent_qq_worker_allowed_emails", frozenset({"*"}))
mixed_parent = submit_routed_job(
    ["first@qq.com", "second@example.com", "third@foxmail.com"],
    2,
    owner_id="mixed-owner",
    owner_email="mixed-owner@example.com",
    job_id="mixedparent01",
)
mixed_children = job_store.children(mixed_parent.id)
assert mixed_parent.execution_target == "aggregate"
assert {child.execution_target for child in mixed_children} == {"local", "tencent_qq"}
assert [child for child in mixed_children if child.execution_target == "tencent_qq"][0].emails == [
    "first@qq.com",
    "third@foxmail.com",
]
assert [child for child in mixed_children if child.execution_target == "local"][0].emails == [
    "second@example.com"
]
for child in mixed_children:
    child.status = "completed"
    child.started_at = utc_now()
    child.finished_at = utc_now()
    child.results = [
        {"email": email, "original_index": index, "deliverable": True}
        for index, email in enumerate(child.emails)
    ]
    job_store.persist(child)
    sync_parent_job(child)
mixed_parent = job_store.get(mixed_parent.id)
assert mixed_parent is not None
assert mixed_parent.status == "completed"
assert [result["email"] for result in mixed_parent.results] == [
    "first@qq.com",
    "second@example.com",
    "third@foxmail.com",
]
assert mixed_parent.csv_path is not None and mixed_parent.csv_path.exists()
assert mixed_parent.id in [job.id for job in job_store.list_recent("mixed-owner")]
assert all(job.parent_id is None for job in job_store.list_recent("mixed-owner"))

stopped_parent = submit_routed_job(
    ["stop@qq.com", "stop@example.com"],
    1,
    owner_id="mixed-owner",
    owner_email="mixed-owner@example.com",
    job_id="mixedstop001",
)
assert job_store.stop(stopped_parent.id).status == "stopped"
assert all(child.status == "stopped" for child in job_store.children(stopped_parent.id))
object.__setattr__(
    settings, "tencent_qq_worker_allowed_emails", frozenset({"smoke@example.com"})
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
    assert guest.post("/api/workers/cloudstudio/probe").status_code == 401
    cloudstudio_probe = guest.post(
        "/api/workers/cloudstudio/probe",
        headers={
            "X-Verigo-CloudStudio-Probe-Token": "smoke-cloudstudio-probe-token",
            "X-Verigo-CloudStudio-Workspace-Key": "smoke-workspace",
        },
    )
    assert cloudstudio_probe.status_code == 200, cloudstudio_probe.text
    assert cloudstudio_probe.json() == {
        "status": "accepted", "workspace_key": "smoke-workspace"
    }

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
    resumed_guest_job = guest.post(
        f"/api/jobs/{guest_single.json()['id']}/resume",
        headers={"X-Job-Token": guest_single.json()["access_token"]},
    )
    assert resumed_guest_job.status_code == 202, resumed_guest_job.text
    assert resumed_guest_job.json()["id"] != guest_single.json()["id"]
    assert resumed_guest_job.json()["status"] == "queued"
    job_store.stop(resumed_guest_job.json()["id"])
    yahoo_single = guest.post("/api/verify/single", json={"email": "person@yahoo.co.uk"})
    assert yahoo_single.status_code == 202, yahoo_single.text
    assert yahoo_single.json()["status"] == "completed"
    yahoo_single_results = guest.get(
        f"/api/jobs/{yahoo_single.json()['id']}/results?limit=50",
        headers={"X-Job-Token": yahoo_single.json()["access_token"]},
    )
    assert yahoo_single_results.status_code == 200, yahoo_single_results.text
    yahoo_result = yahoo_single_results.json()["items"][0]
    assert yahoo_result["verification_method"] == "不支持验证"
    assert yahoo_result["skipped"] is True
    object.__setattr__(settings, "tencent_qq_worker_allowed_emails", frozenset({"*"}))
    guest_qq = guest.post("/api/verify/single", json={"email": "public-user@qq.com"})
    object.__setattr__(
        settings, "tencent_qq_worker_allowed_emails", frozenset({"smoke@example.com"})
    )
    assert guest_qq.status_code == 202, guest_qq.text
    assert guest.post("/api/workers/tencent-qq/claim").status_code == 401
    worker_claim = guest.post(
        "/api/workers/tencent-qq/claim?wait_seconds=0",
        headers={
            "X-Verigo-Worker-Token": "smoke-tencent-worker-token",
            "X-Verigo-Worker-Id": "smoke-cloudstudio",
        },
    )
    assert worker_claim.status_code == 200, worker_claim.text
    assert worker_claim.json()["job"]["id"] == guest_qq.json()["id"]
    assert job_store.worker_runtime("tencent_qq").worker_id == "smoke-cloudstudio"
    stopped_qq_job = guest.post(
        f"/api/jobs/{guest_qq.json()['id']}/stop",
        headers={"X-Job-Token": guest_qq.json()["access_token"]},
    )
    assert stopped_qq_job.status_code == 200, stopped_qq_job.text


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
    yahoo_candidates = account.post(
        "/api/discovery/candidates",
        json={"first_name": "Ming", "last_name": "Wang", "domain": "yahoo.co.jp"},
    )
    assert yahoo_candidates.status_code == 422, yahoo_candidates.text
    assert account.get("/api/auth/me").json()["credits"] == 10

    yahoo_batch = account.post(
        "/api/jobs",
        json={"emails": ["person@ymail.com", "other@example.com"], "worker_count": 2},
    )
    assert yahoo_batch.status_code == 202, yahoo_batch.text
    assert yahoo_batch.json()["completed"] == 1
    assert yahoo_batch.json()["total"] == 2
    yahoo_batch_results = account.get(f"/api/jobs/{yahoo_batch.json()['id']}/results?limit=50")
    assert yahoo_batch_results.status_code == 200, yahoo_batch_results.text
    assert yahoo_batch_results.json()["items"][0]["email"] == "person@ymail.com"
    assert yahoo_batch_results.json()["items"][0]["verification_method"] == "不支持验证"
    assert account.get("/api/auth/me").json()["credits"] == 9
    auth_store.refund_credits(user_id, 1, f"verification:{yahoo_batch.json()['id']}")
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

    credit_target = auth_store.create_user(
        "manual-credit@example.com", "correct-horse-2026"
    )
    granted = admin_account.post(
        "/api/admin/credits/grant",
        json={
            "email": "manual-credit@example.com",
            "credits": 25,
            "note": "manual payment smoke test",
        },
    )
    assert granted.status_code == 200, granted.text
    assert granted.json()["email"] == "manual-credit@example.com"
    assert granted.json()["delta"] == 25
    assert granted.json()["credits"] == 25
    assert granted.json()["paid_credits"] == 25
    with auth_store._connect() as connection:
        ledger = connection.execute(
            "SELECT delta, kind FROM credit_ledger WHERE reference=?",
            (granted.json()["reference"],),
        ).fetchone()
        audit = connection.execute(
            "SELECT user_id, adjusted_by_user_id, delta, note FROM admin_credit_adjustments WHERE reference=?",
            (granted.json()["reference"],),
        ).fetchone()
    assert ledger == (25, "admin_credit_grant")
    assert audit == (
        credit_target.id,
        admin_id,
        25,
        "manual payment smoke test",
    )
    assert admin_account.post(
        "/api/admin/credits/grant",
        json={"email": "missing@example.com", "credits": 1},
    ).status_code == 422
    deducted = admin_account.post(
        "/api/admin/credits/deduct",
        json={"email": "manual-credit@example.com", "credits": 7, "note": "refund smoke test"},
    )
    assert deducted.status_code == 200, deducted.text
    assert deducted.json()["delta"] == -7
    assert deducted.json()["credits"] == 18
    assert admin_account.post(
        "/api/admin/credits/deduct",
        json={"email": "manual-credit@example.com", "credits": 19},
    ).status_code == 422
    notifications, unread_count = auth_store.list_notifications(credit_target.id)
    assert unread_count == 2
    assert [item["kind"] for item in notifications] == ["credit_deduction", "credit_grant"]
    with TestClient(app) as credited_account:
        logged_in = credited_account.post(
            "/api/auth/login",
            json={"account": "manual-credit@example.com", "password": "correct-horse-2026"},
        )
        assert logged_in.status_code == 200, logged_in.text
        inbox = credited_account.get("/api/notifications")
        assert inbox.status_code == 200, inbox.text
        assert inbox.json()["unread_count"] == 2
        assert credited_account.post("/api/notifications/read").status_code == 204
        assert credited_account.get("/api/notifications").json()["unread_count"] == 0
    assert auth_store.delete_user(credit_target.id) == []
    with auth_store._connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM admin_credit_adjustments WHERE user_id=?", (credit_target.id,)
        ).fetchone() is None


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
assert config["max_attempts"] == 1
assert config["max_mx_hosts"] == 1
assert legacy.smtp_gate_capacity("mx1.qq.com") == 1
assert verifier._handle_qq_response(250, b"OK", config, 0)[0] is True
assert verifier._handle_qq_response(550, b"Mailbox not found", config, 0)[0] is False
assert verifier._handle_qq_response(550, b"Access denied by policy", config, config["max_attempts"] - 1)[0] is None

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
assert closed is None
assert "SMTP连接被服务器关闭" in closed_detail

print("backend smoke: ok")
