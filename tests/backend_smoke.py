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
os.environ["VERIGO_MONITOR_TOKEN"] = "smoke-monitor-token"
os.environ["VERIGO_CLOUDSTUDIO_PROBE_TOKEN"] = "smoke-cloudstudio-probe-token"
os.environ["VERIGO_TENCENT_QQ_WORKER_TOKEN"] = "smoke-tencent-worker-token"
os.environ["VERIGO_TENCENT_QQ_WORKER_ENABLED"] = "true"
os.environ["VERIGO_TENCENT_QQ_WORKER_ALLOWED_EMAILS"] = "smoke@example.com"
os.environ["VERIGO_GMAIL_WORKER_TOKEN"] = "smoke-gmail-worker-token"
os.environ["VERIGO_GMAIL_WORKER_ENABLED"] = "true"
os.environ["VERIGO_GMAIL_WORKER_ALLOWED_EMAILS"] = "smoke@example.com"
os.environ["VERIGO_CODEARTS_WORKER_TOKEN"] = "smoke-codearts-worker-token"
os.environ["VERIGO_CODEARTS_WORKER_ENABLED"] = "true"
os.environ["VERIGO_CODEARTS_WORKER_ALLOWED_EMAILS"] = "smoke@example.com"

from fastapi.testclient import TestClient
from openpyxl import Workbook

import app.api.auth as auth_api
from app.api.routes import (
    email_execution_target,
    gmail_target,
    remote_worker_count,
    serialize_job,
    submit_routed_job,
    submit_stopped_job_continuation,
    tencent_qq_target,
)
from app.api.schemas import CreateJobRequest
from app.config import settings
from app.core.legacy import load_legacy_module
from app.core.result_retry import (
    is_recipient_mailbox_full,
    is_retryable_smtp_result,
    is_smtp_greylisted,
    is_temporary_smtp_452,
    smtp_permanent_status,
    smtp_temporary_status,
)
from app.core.security import hash_password, token_hash
from app.db.auth import auth_store
from app.db.jobs import Job, job_store, utc_now
from app.main import app
from app.tasks.verification import (
    finish_background_retry,
    finish_background_retry_failure,
    finish_initial_job,
    normalize_result,
    finalize_temporary_smtp_results,
    requeue_recent_single_temporary_jobs,
    schedule_greylist_retry,
    sync_parent_job,
    schedule_remote_temporary_retry,
    job_progress,
    verification_tasks,
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


visible_progress_job = verification_tasks.submit(
    ["waiting-one@example.com", "waiting-two@example.com"],
    worker_count=1,
    execution_target="progress-smoke",
)
assert len(visible_progress_job.results) == 2
assert all(item["progress_state"] == "pending" for item in visible_progress_job.results)
assert job_progress(visible_progress_job) == (0, 2, 0.0)
claimed_progress_job = job_store.claim_next("progress-worker", "progress-smoke")
assert claimed_progress_job is not None
assert all(item["progress_state"] == "verifying" for item in claimed_progress_job.results)
assert job_progress(claimed_progress_job) == (0, 2, 0.0)

batched_remote_job = verification_tasks.submit(
    [f"batch-{index}@example{index}.com" for index in range(25)],
    worker_count=1,
    execution_target="gmail",
)
batched_remote_job = job_store.claim_remote_lease(
    "batched-worker", "gmail", shard_size=25
)
assert batched_remote_job is not None
with TestClient(app) as remote_client:
    headers = {
        "X-Verigo-Worker-Token": "smoke-gmail-worker-token",
        "X-Verigo-Worker-Id": "batched-worker",
    }
    for index, email in enumerate(batched_remote_job.emails):
        response = remote_client.post(
            f"/api/workers/gmail/jobs/{batched_remote_job.id}/results",
            headers=headers,
            json={"lease_id": batched_remote_job.lease_id, "results": [{
                "email": email,
                "original_index": index,
                "valid": True,
                "deliverable": True,
                "verification_method": "smoke",
                "smtp_result": "smoke",
                "message": "smoke",
            }]},
        )
        assert response.status_code == 200
        assert response.json()["persisted"] == 1
        persisted = job_store.get(batched_remote_job.id)
        assert persisted is not None
        assert persisted.results[index]["progress_state"] == "completed"
persisted = job_store.get(batched_remote_job.id)
assert persisted is not None
assert all(result.get("progress_state") != "verifying" for result in persisted.results)

failed_progress_job = verification_tasks.submit(
    ["failed-one@example.com", "failed-two@example.com"],
    worker_count=1,
    execution_target="failed-progress-smoke",
)
assert job_store.fail_queued_target("failed-progress-smoke", "worker failed to start") == 1
failed_progress_job = job_store.get(failed_progress_job.id)
assert failed_progress_job is not None
assert all(item["progress_state"] == "failed" for item in failed_progress_job.results)

legacy_failed_progress_job = Job(
    id="legacy-failed-progress", emails=["legacy@example.com"], worker_count=1,
    status="failed", error="worker failed to start",
)
job_store.add(legacy_failed_progress_job)
assert job_store.reconcile_failed_job_results() >= 1
legacy_failed_progress_job = job_store.get(legacy_failed_progress_job.id)
assert legacy_failed_progress_job is not None
assert legacy_failed_progress_job.results[0]["progress_state"] == "failed"

completed_result_job = Job(
    id="completed-result-preserved",
    emails=["completed@example.com", "pending@example.com"],
    worker_count=1,
    status="failed",
    results=[
        {"email": "completed@example.com", "valid": True, "deliverable": True},
        {"email": "pending@example.com", "progress_state": "pending"},
    ],
)
job_store.add(completed_result_job)
job_store.mark_unfinished_results_failed(completed_result_job, "worker failed")
preserved = job_store.get(completed_result_job.id)
assert preserved is not None
assert preserved.results[0]["valid"] is True
assert preserved.results[0]["deliverable"] is True
assert preserved.results[1]["progress_state"] == "failed"


assert tencent_qq_target(["person@qq.com"], "smoke@example.com") == "tencent_qq"
assert tencent_qq_target(["person@qq.com"], "other@example.com") == "local"
assert email_execution_target("person@qq.com", "other@example.com") == "tencent_qq"
assert tencent_qq_target(["person@163.com"], "smoke@example.com") == "tencent_qq"
assert tencent_qq_target(["person@company.cn"], "smoke@example.com") == "tencent_qq"
assert tencent_qq_target(["person@example.com"], "smoke@example.com") == "local"
assert gmail_target(["person@gmail.com"], "smoke@example.com") == "gmail"
assert gmail_target(["person@gmail.com"], "other@example.com") == "local"
assert gmail_target(["person@outlook.com"], "smoke@example.com") == "gmail"
assert gmail_target(["person@company.de"], "smoke@example.com") == "gmail"
assert gmail_target(["person@example.com"], "smoke@example.com") == "gmail"
assert remote_worker_count("tencent_qq", 8) == 4
assert remote_worker_count("gmail", 8) == 8
assert remote_worker_count("codearts", 32) == 16
object.__setattr__(settings, "cloudstudio_domestic_worker_enabled", True)
assert email_execution_target("person@163.com", "smoke@example.com") == "cloudstudio_domestic"
assert email_execution_target("person@qq.com", "smoke@example.com") == "tencent_qq"
assert email_execution_target("sales@company.com", "smoke@example.com") == "codearts"
object.__setattr__(settings, "cloudstudio_domestic_worker_enabled", False)
assert is_temporary_smtp_452({"smtp_result": "452 temporary mailbox failure"})
assert is_temporary_smtp_452({"message": "452 暂时无法确认"})
assert not is_temporary_smtp_452({"smtp_result": "550 mailbox unavailable"})
gmail_full = {"smtp_result": "452 4.2.2 The email account that you tried to reach is over quota"}
assert is_recipient_mailbox_full(gmail_full)
assert not is_retryable_smtp_result(gmail_full)
assert not is_temporary_smtp_452(gmail_full)
assert smtp_temporary_status({"smtp_result": "421 service not available"}) == "421"
assert smtp_temporary_status({"smtp_result": "450 greylisted"}) == "450"
assert smtp_temporary_status({"smtp_result": "451 local error"}) == "451"
assert smtp_temporary_status({"smtp_result": "455 parameters unavailable"}) == "455"
assert smtp_temporary_status({"smtp_result": "550 mailbox unavailable"}) is None
assert smtp_permanent_status({"smtp_result": "550 mailbox unavailable"}) == "550"
assert smtp_permanent_status({"smtp_result": "554 transaction failed"}) == "554"
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

for code in ("550", "553", "554"):
    permanent = normalize_result({
        "email": "permanent@example.com",
        "valid": True,
        "deliverable": None,
        "checks": {"format": True, "domain": True, "mx": True, "smtp": None},
        "smtp_result": f"RCPT TO returned {code} permanent rejection",
    })
    assert permanent["deliverable"] is False
    assert permanent["valid"] is False
    assert permanent["checks"]["smtp"] is False
    assert permanent["smtp_result"] == f"{code} 不可投递"

legacy_permanent_summary = serialize_job(Job(
    id="legacypermanent01",
    emails=["permanent@example.com"],
    worker_count=1,
    status="completed",
    results=[{
        "email": "permanent@example.com",
        "deliverable": None,
        "smtp_result": "554 transaction failed",
    }],
)).summary
assert legacy_permanent_summary is not None
assert legacy_permanent_summary.undeliverable == 1
assert legacy_permanent_summary.unknown == 0

legacy_outlook = normalize_result({
    "email": "person@outlook.com",
    "deliverable": True,
    "verification_method": "microsoft_api",
    "smtp_result": "微软接口确认账号存在 [接口A:IfExistsResult=5 | 接口B:account=MSAccount]",
})
assert legacy_outlook["smtp_result"] == "Outlook 邮箱已确认可投递"
assert legacy_outlook["message"] == "Outlook 邮箱已确认可投递"
assert legacy_outlook["verification_method"] == "Outlook 账号验证"


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

original_resolve = legacy_module.dns.resolver.resolve
try:
    def mx_only_resolver(_domain, record_type):
        if record_type == "MX":
            return [object()]
        raise legacy_module.dns.resolver.NoAnswer()

    legacy_module.dns.resolver.resolve = mx_only_resolver
    mx_only_verifier = legacy_module.EmailVerifier()
    assert mx_only_verifier.check_domain_exists("mx-only.example")
finally:
    legacy_module.dns.resolver.resolve = original_resolve

try:
    legacy_module.dns.resolver.resolve = lambda *_args: (_ for _ in ()).throw(
        legacy_module.dns.resolver.NXDOMAIN()
    )
    dns_verifier = legacy_module.EmailVerifier()
    assert dns_verifier.check_domain_status("missing.example") == "nxdomain"
    assert dns_verifier.get_mx_record_status("missing.example") == ("nxdomain", [])
    missing_result = dns_verifier.verify_email_comprehensive("person@missing.example")
    assert missing_result["failure_reason"] == "domain_nxdomain"
    assert missing_result["retry_policy"] == "never"
    assert not is_retryable_smtp_result(missing_result)

    legacy_module.dns.resolver.resolve = lambda *_args: (_ for _ in ()).throw(
        legacy_module.dns.resolver.NoNameservers()
    )
    transient_verifier = legacy_module.EmailVerifier()
    assert transient_verifier.check_domain_status("dns-failure.example") == "transient"
    assert transient_verifier.get_mx_record_status("dns-failure.example") == ("transient", [])
    transient_result = transient_verifier.verify_email_comprehensive("person@dns-failure.example")
    assert transient_result["failure_reason"] == "dns_transient"
    assert transient_result["retry_policy"] == "delayed"
    assert is_retryable_smtp_result(transient_result)
finally:
    legacy_module.dns.resolver.resolve = original_resolve

finalize_temporary_smtp_results([greylisted])
assert greylisted["deliverable"] is None
assert greylisted["valid"] is True
assert greylisted["greylist_retry_exhausted"] is True

ordinary_temporary = normalize_result({
    "email": "temporary@example.com", "deliverable": None,
    "smtp_result": "452 temporary SMTP failure",
})
finalize_temporary_smtp_results([ordinary_temporary])
assert ordinary_temporary["deliverable"] is False
assert ordinary_temporary["valid"] is False
assert ordinary_temporary["temporary_retries_exhausted"] is True
assert "连续 3 次" in ordinary_temporary["smtp_result"]

mailbox_full = normalize_result({
    "email": "full@gmail.com", "deliverable": None,
    "smtp_result": "452 4.2.2 The email account that you tried to reach is over quota",
})
assert mailbox_full["deliverable"] is False
assert mailbox_full["valid"] is False
assert mailbox_full["delivery_block_reason"] == "mailbox_full"
assert "收件箱容量已满" in mailbox_full["smtp_result"]
finalize_temporary_smtp_results([mailbox_full])
assert mailbox_full["delivery_block_reason"] == "mailbox_full"
assert mailbox_full.get("temporary_retries_exhausted") is not True

greylist_retry_job = Job(
    id="smoketemp005", emails=["pengjie.ai@porsche.cn"], worker_count=1,
    status="running", results=[normalize_result({
        "email": "pengjie.ai@porsche.cn", "deliverable": None,
        "smtp_result": "450 Sender address rejected: Greylisted",
    })],
)
assert schedule_greylist_retry(greylist_retry_job)
assert greylist_retry_job.status == "queued"
assert greylist_retry_job.deferred_retry_at is not None

remote_retry_job = Job(
    id="smoketemp001", emails=["pengjie.ai@porsche.cn"], worker_count=1,
    status="running", results=[normalize_result({
        "email": "pengjie.ai@porsche.cn", "deliverable": None,
        "smtp_result": "450 temporary SMTP failure",
    })], worker_id="smoke-worker",
)
assert schedule_remote_temporary_retry(remote_retry_job)
assert remote_retry_job.status == "queued"
assert remote_retry_job.deferred_retry_at is not None
assert remote_retry_job.temporary_retry_attempts == 1

stale_single_temporary = Job(
    id="smoketemp003", emails=["pengjie.ai@porsche.cn"], worker_count=1,
    status="completed", finished_at=utc_now(), results=[normalize_result({
        "email": "pengjie.ai@porsche.cn", "deliverable": False,
        "smtp_result": "450 temporary SMTP failure",
    })],
)
job_store.add(stale_single_temporary)
assert requeue_recent_single_temporary_jobs() == 1
assert job_store.get(stale_single_temporary.id).status == "completed"
stale_retry_children = job_store.retry_children(stale_single_temporary.id)
assert len(stale_retry_children) == 1
assert stale_retry_children[0].status == "queued"
assert stale_retry_children[0].deferred_retry_at is not None
assert requeue_recent_single_temporary_jobs() == 0
assert len(job_store.retry_children(stale_single_temporary.id)) == 1

exhausted_dns_retry = normalize_result({
    "email": "dns-retry@example.com", "deliverable": None,
    "checks": {"format": True, "domain": None, "mx": None, "smtp": None},
    "smtp_result": "DNS 查询暂时失败，未发起SMTP验证",
    "failure_stage": "dns",
    "failure_reason": "dns_transient",
    "retry_policy": "delayed",
})
finalize_temporary_smtp_results([exhausted_dns_retry])
assert exhausted_dns_retry["transient_retries_exhausted"] is True
assert exhausted_dns_retry["retry_policy"] == "never"
exhausted_dns_job = Job(
    id="smoketempdns01", emails=["dns-retry@example.com"], worker_count=1,
    status="completed", finished_at=utc_now(), results=[exhausted_dns_retry],
)
job_store.add(exhausted_dns_job)
assert requeue_recent_single_temporary_jobs() == 0
assert not job_store.retry_children(exhausted_dns_job.id)

retry_notice_owner = auth_store.create_user("retry-notice@example.com", "correct-horse-2026")
background_parent = Job(
    id="smokebackground01", emails=["later@example.com"], worker_count=1, owner_id=retry_notice_owner.id,
    status="running", results=[normalize_result({
        "email": "later@example.com", "deliverable": None,
        "smtp_result": "452 temporary SMTP failure",
    })],
)
job_store.add(background_parent)
finish_initial_job(background_parent)
background_parent = job_store.get(background_parent.id)
assert background_parent is not None and background_parent.status == "completed"
assert background_parent.results[0]["retry_at"]
assert serialize_job(background_parent).retry_at is not None
background_retry = job_store.retry_children(background_parent.id)[0]
background_retry.status = "completed"
background_retry.results = [normalize_result({
    "email": "later@example.com", "deliverable": False,
    "smtp_result": "550 mailbox unavailable",
})]
job_store.persist(background_retry)
finish_background_retry(background_retry)
background_parent = job_store.get(background_parent.id)
assert background_parent is not None
assert background_parent.results[0]["deliverable"] is False
assert background_parent.results[0]["retry_updated"] is True
assert background_parent.results[0]["retry_state"] == "completed"
assert "retry_at" not in background_parent.results[0]
assert retry_notice_owner.id == background_parent.owner_id
retry_notifications, retry_unread, _ = auth_store.list_notifications(
    background_parent.owner_id or "", limit=10
)
assert retry_unread >= 1
retry_notice = next(item for item in retry_notifications if item["kind"] == "verification_review")
assert retry_notice["target_job_id"] == background_parent.id
assert retry_notice["target_email"] == "later@example.com"
assert retry_notice["target_result_index"] == 0
assert job_store.clear_result_review_update(background_parent.id, 0) is True
auth_store.mark_result_notifications_read(background_parent.owner_id or "", background_parent.id, 0)
assert job_store.get(background_parent.id).results[0].get("retry_updated") is None

failed_retry_parent = Job(
    id="smokebackground02", emails=["retry-failure@example.com"], worker_count=1,
    status="completed", results=[normalize_result({
        "email": "retry-failure@example.com", "deliverable": None,
        "smtp_result": "452 temporary SMTP failure",
        "retry_state": "scheduled", "retry_at": utc_now().isoformat(),
    })],
)
job_store.add(failed_retry_parent)
failed_retry = Job(
    id="smokebackground03", emails=["retry-failure@example.com"], worker_count=1,
    status="failed", retry_parent_id=failed_retry_parent.id,
    temporary_retry_attempts=settings.temporary_smtp_immediate_retries,
)
job_store.add(failed_retry)
finish_background_retry_failure(failed_retry, "worker unavailable")
failed_retry_parent = job_store.get(failed_retry_parent.id)
assert failed_retry_parent is not None
assert failed_retry_parent.results[0]["deliverable"] is None
assert failed_retry_parent.results[0]["retry_state"] == "failed"
assert "retry_at" not in failed_retry_parent.results[0]

completed_retry_notice = Job(
    id="smoketemp004", emails=["confirmed@example.com"], worker_count=1,
    status="completed", error="检测到未完成的 SMTP 临时结果，已恢复自动重试",
)
job_store.add(completed_retry_notice)
assert job_store.clear_completed_retry_notices() == 1
assert job_store.get(completed_retry_notice.id).error is None

job_store.cache_results([{
    "email": "mx-only-cache@example.com", "deliverable": False,
    "checks": {"domain": False, "mx": False, "smtp": False},
    "smtp_result": "域名不存在",
}])
assert "mx-only-cache@example.com" not in job_store.cached_results(
    ["mx-only-cache@example.com"]
)

legacy_deferred_job = Job(
    id="smoketemp002", emails=["pengjie.ai@porsche.cn"], worker_count=1,
    status="queued", deferred_retry_at=utc_now(),
    error="检测到 1 个 SMTP 临时响应，系统将在 12:00 自动复核",
)
job_store.add(legacy_deferred_job)
assert job_store.release_legacy_deferred_retries() == 1
assert job_store.get(legacy_deferred_job.id).deferred_retry_at is None

object.__setattr__(settings, "tencent_qq_worker_allowed_emails", frozenset({"*"}))
assert tencent_qq_target(["person@qq.com"], "other@example.com") == "tencent_qq"
assert tencent_qq_target(["person@qq.com"], None) == "tencent_qq"
assert tencent_qq_target(["person@163.com"], None) == "tencent_qq"
assert tencent_qq_target(["person@example.com"], None) == "local"
assert gmail_target(["person@outlook.com"], "smoke@example.com") == "gmail"
object.__setattr__(
    settings, "tencent_qq_worker_allowed_emails", frozenset({"smoke@example.com"})
)

object.__setattr__(settings, "tencent_qq_worker_allowed_emails", frozenset({"*"}))
object.__setattr__(settings, "gmail_worker_allowed_emails", frozenset({"*"}))
large_domestic_emails = [f"domestic-{index}@163.com" for index in range(5001)]
assert len(CreateJobRequest(emails=large_domestic_emails).emails) == 5001
large_domestic_parent = submit_routed_job(
    large_domestic_emails,
    2,
    owner_id="mixed-owner",
    owner_email="mixed-owner@example.com",
    job_id="largedomestic01",
)
large_domestic_children = job_store.children(large_domestic_parent.id)
assert large_domestic_parent.execution_target == "aggregate"
assert [child.execution_target for child in large_domestic_children] == ["tencent_qq", "tencent_qq"]
assert [len(child.emails) for child in large_domestic_children] == [5000, 1]
assert job_store.stop(large_domestic_parent.id).status == "stopped"
large_domestic_continuation = submit_stopped_job_continuation(
    job_store.get(large_domestic_parent.id)
)
assert large_domestic_continuation.id == large_domestic_parent.id
assert [len(child.emails) for child in job_store.children(large_domestic_continuation.id)] == [5000, 1]
assert job_store.stop(large_domestic_continuation.id).status == "stopped"
large_stop_on_deliverable = submit_routed_job(
    large_domestic_emails,
    2,
    owner_id="mixed-owner",
    owner_email="mixed-owner@example.com",
    stop_on_deliverable=True,
    job_id="largestopdeliver01",
)
assert large_stop_on_deliverable.execution_target == "local"
assert job_store.stop(large_stop_on_deliverable.id).status == "stopped"
single_target_stop_on_deliverable = submit_routed_job(
    ["first@gmail.com", "second@gmail.com"],
    2,
    owner_id="mixed-owner",
    owner_email="mixed-owner@example.com",
    stop_on_deliverable=True,
    job_id="singlestopdeliver01",
)
assert single_target_stop_on_deliverable.execution_target == "local"
assert job_store.stop(single_target_stop_on_deliverable.id).status == "stopped"
yahoo_mixed_parent = submit_routed_job(
    ["skip@yahoo.com", "first@qq.com", "second@gmail.com", "third@example.com"],
    2,
    owner_id="mixed-owner",
    owner_email="mixed-owner@example.com",
    job_id="mixedyahoo001",
)
yahoo_mixed_children = job_store.children(yahoo_mixed_parent.id)
assert {child.execution_target for child in yahoo_mixed_children} == {
    "unsupported", "gmail", "tencent_qq"
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
    "gmail", "tencent_qq"
}
assert next(child for child in mixed_three_way_children if child.execution_target == "tencent_qq").emails == ["first@qq.com"]
assert next(child for child in mixed_three_way_children if child.execution_target == "gmail").emails == [
    "second@gmail.com", "third@example.com", "fourth@googlemail.com"
]
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
    ["first@qq.com", "second@example.com", "third@foxmail.com", "fourth@163.com"],
    2,
    owner_id="mixed-owner",
    owner_email="mixed-owner@example.com",
    job_id="mixedparent01",
)
mixed_children = job_store.children(mixed_parent.id)
assert mixed_parent.execution_target == "aggregate"
assert {child.execution_target for child in mixed_children} == {"local", "tencent_qq"}
mixed_tencent_children = [
    child for child in mixed_children if child.execution_target == "tencent_qq"
]
assert [child.emails for child in mixed_tencent_children] == [
    ["first@qq.com", "third@foxmail.com"],
    ["fourth@163.com"],
]
assert [child.worker_count for child in mixed_tencent_children] == [1, 2]
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
    "fourth@163.com",
]
assert mixed_parent.csv_path is not None and mixed_parent.csv_path.exists()
assert mixed_parent.id in [job.id for job in job_store.list_recent("mixed-owner")]
assert all(job.parent_id is None for job in job_store.list_recent("mixed-owner"))

# A worker can report a temporary SMTP response twice: first to schedule the
# retry, then after it completes. The visible parent must retain the schedule.
retry_parent = submit_routed_job(
    ["retry@qq.com", "retry@example.com"],
    2,
    owner_id="mixed-owner",
    owner_email="mixed-owner@example.com",
    job_id="retryparent01",
)
retry_child = next(
    child for child in job_store.children(retry_parent.id)
    if child.execution_target == "tencent_qq"
)
retry_child.results = [{
    "email": "retry@qq.com", "original_index": 0, "deliverable": None,
    "smtp_result": "452 Mailbox temporarily unavailable", "retry_state": "scheduled",
    "retry_attempt": 1, "retry_max_attempts": 3, "retry_at": "2030-01-01T00:00:00+00:00",
}]
job_store.persist(retry_child)
sync_parent_job(retry_child)
retry_parent = job_store.get(retry_parent.id)
assert retry_parent is not None
assert retry_parent.results[0]["retry_at"] == "2030-01-01T00:00:00+00:00"
assert retry_parent.results[0]["retry_attempt"] == 1
job_store.stop(retry_parent.id)

fallback_job = verification_tasks.submit(
    ["fallback@example.com"],
    1,
    owner_id="mixed-owner",
    job_id="fallbackgmail001",
    execution_target="gmail-fallback",
)
assert fallback_job.status == "queued"
assert job_store.reroute_queued_jobs("gmail-fallback", "local", "Remote worker unavailable") == 1
assert job_store.get(fallback_job.id).execution_target == "local"

stopped_parent = submit_routed_job(
    ["stop@qq.com", "stop@163.com", "stop@example.com"],
    3,
    owner_id="mixed-owner",
    owner_email="mixed-owner@example.com",
    job_id="mixedstop001",
)
assert job_store.stop(stopped_parent.id).status == "stopped"
assert all(child.status == "stopped" for child in job_store.children(stopped_parent.id))
stopped_continuation = submit_stopped_job_continuation(job_store.get(stopped_parent.id))
assert stopped_continuation.id == stopped_parent.id
stopped_continuation_children = job_store.children(stopped_continuation.id)
assert [
    (child.emails, child.worker_count)
    for child in stopped_continuation_children
    if child.execution_target == "tencent_qq"
] == [(["stop@qq.com"], 1), (["stop@163.com"], 3)]
assert job_store.stop(stopped_continuation.id).status == "stopped"
object.__setattr__(
    settings, "tencent_qq_worker_allowed_emails", frozenset({"smoke@example.com"})
)


with TestClient(app) as guest:
    health = guest.get("/api/health")
    assert health.status_code == 200
    health_payload = health.json()
    assert health_payload == {"status": "ok", "database": "ok"}
    assert guest.get("/api/internal/readiness").status_code == 401
    readiness = guest.get(
        "/api/internal/readiness",
        headers={"X-Verigo-Monitor-Token": "smoke-monitor-token"},
    )
    assert readiness.status_code == 200, readiness.text
    assert readiness.json()["database"] == "ok"
    assert "pending_results" in readiness.json()
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
    assert resumed_guest_job.json()["id"] == guest_single.json()["id"]
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
    assert worker_claim.json()["job"]["worker_count"] == 1
    assert job_store.worker_runtime("tencent_qq").worker_id == "smoke-cloudstudio"
    stopped_qq_job = guest.post(
        f"/api/jobs/{guest_qq.json()['id']}/stop",
        headers={"X-Job-Token": guest_qq.json()["access_token"]},
    )
    assert stopped_qq_job.status_code == 200, stopped_qq_job.text
    object.__setattr__(settings, "tencent_qq_worker_allowed_emails", frozenset({"*"}))
    remote_parallel_job = submit_routed_job(
        ["parallel@163.com"],
        8,
        owner_id="remote-parallel-owner",
        owner_email="remote-parallel-owner@example.com",
        job_id="remoteparallel01",
    )
    object.__setattr__(
        settings, "tencent_qq_worker_allowed_emails", frozenset({"smoke@example.com"})
    )
    parallel_claim = guest.post(
        "/api/workers/tencent-qq/claim?wait_seconds=0",
        headers={
            "X-Verigo-Worker-Token": "smoke-tencent-worker-token",
            "X-Verigo-Worker-Id": "smoke-cloudstudio",
        },
    )
    assert parallel_claim.status_code == 200, parallel_claim.text
    assert parallel_claim.json()["job"]["id"] == remote_parallel_job.id
    assert parallel_claim.json()["job"]["worker_count"] == 4
    assert job_store.stop(remote_parallel_job.id).status == "stopped"
    cloudshell_fast_job = verification_tasks.submit(
        ["fast@company.de"],
        8,
        owner_id="cloudshell-fast-owner",
        job_id="cloudshellfast01",
        execution_target="gmail",
    )
    cloudshell_claim = guest.post(
        "/api/workers/gmail/claim?wait_seconds=0",
        headers={
            "X-Verigo-Worker-Token": "smoke-gmail-worker-token",
            "X-Verigo-Worker-Id": "smoke-cloudshell",
        },
    )
    assert cloudshell_claim.status_code == 200, cloudshell_claim.text
    assert cloudshell_claim.json()["job"]["id"] == cloudshell_fast_job.id
    assert cloudshell_claim.json()["job"]["worker_count"] == 8
    assert job_store.stop(cloudshell_fast_job.id).status == "stopped"


auth_store.check_rate_limit("persistent-rate-limit-smoke", limit=1, window_seconds=3600)
try:
    auth_store.check_rate_limit("persistent-rate-limit-smoke", limit=1, window_seconds=3600)
except ValueError:
    pass
else:
    raise AssertionError("authentication attempt limits must persist in the database")


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

    # New registrations must stay in the guided activation flow until one
    # actual single-email verification has completed.
    assert verified_user["onboarding_step"] == "first_verification"

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
    activation_job_id = first_free.json()["id"]
    activation_user = account.get("/api/auth/me").json()
    assert activation_user["onboarding_step"] == "verification_in_progress"
    assert activation_user["activation_job_id"] == activation_job_id
    activation_job = job_store.get(activation_job_id)
    assert activation_job is not None
    activation_job.status = "completed"
    job_store.persist(activation_job)
    activated = account.post(
        "/api/auth/onboarding/activation/complete", json={"job_id": activation_job_id}
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["onboarding_step"] == "completed"

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

    payment_order = auth_store.create_payment_order(user_id, 2)
    before_payment = account.get("/api/auth/me").json()["paid_credits"]
    paid_order = auth_store.complete_payment_order(payment_order["id"])
    assert paid_order["status"] == "paid"
    assert account.get("/api/auth/me").json()["paid_credits"] == before_payment + 200
    # A gateway retry must be idempotent and never credit the order twice.
    assert auth_store.complete_payment_order(payment_order["id"])["status"] == "paid"
    assert account.get("/api/auth/me").json()["paid_credits"] == before_payment + 200

    job_store.add(completed_job("ownedjob0001", owner_id=user_id))
    legacy_jobs = account.get("/api/jobs?limit=20")
    assert legacy_jobs.status_code == 200
    assert isinstance(legacy_jobs.json(), list)
    assert "ownedjob0001" in [job["id"] for job in legacy_jobs.json()]
    jobs = account.get("/api/jobs?offset=0&limit=20")
    assert jobs.status_code == 200
    assert jobs.json()["total"] >= 1
    assert "ownedjob0001" in [job["id"] for job in jobs.json()["items"]]
    completed_history = account.get("/api/jobs?offset=0&limit=20&status=completed&search=check@example.com")
    assert completed_history.status_code == 200
    assert "ownedjob0001" in [job["id"] for job in completed_history.json()["items"]]
    assert account.get("/api/jobs?offset=0&limit=20&status=invalid").status_code == 422
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
    account_list = admin_account.get("/api/admin/accounts/list?offset=0&limit=50")
    assert account_list.status_code == 200, account_list.text
    assert account_list.json()["total"] >= 2
    assert account_list.json()["summary"]["paid_verifications"] >= 18
    assert account_list.json()["summary"]["used_verifications"] >= 0
    assert admin_account.post(
        "/api/admin/credits/deduct",
        json={"email": "manual-credit@example.com", "credits": 19},
    ).status_code == 422
    notifications, unread_count, notification_total = auth_store.list_notifications(credit_target.id)
    assert unread_count == 2
    assert notification_total == 2
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


api_user = auth_store.create_user("api-user@example.com", "correct-horse-2026")
api_code = auth_store.create_email_verification(api_user.id)
auth_store.confirm_email_verification(api_user.id, api_code, network_hash="api-key-test")
with TestClient(app) as api_client:
    assert api_client.get("/api-docs").status_code == 200
    assert api_client.get("/openapi.json").status_code == 200
    login = api_client.post(
        "/api/auth/login",
        json={"account": "api-user@example.com", "password": "correct-horse-2026"},
    )
    assert login.status_code == 200, login.text
    created_key = api_client.post("/api/auth/api-keys", json={"name": "smoke"})
    assert created_key.status_code == 201, created_key.text
    key_payload = created_key.json()
    assert key_payload["token"].startswith("vg_live_")
    assert key_payload["prefix"] == key_payload["token"][:16]
    key_headers = {"Authorization": f"Bearer {key_payload['token']}"}
    api_client.cookies.clear()
    assert api_client.get("/api/jobs", headers=key_headers).status_code == 200
    assert api_client.get("/api/auth/api-keys", headers=key_headers).status_code == 403
    assert api_client.post(
        "/api/auth/login",
        json={"account": "api-user@example.com", "password": "correct-horse-2026"},
    ).status_code == 200
    keys = api_client.get("/api/auth/api-keys")
    assert keys.status_code == 200 and len(keys.json()) == 1
    assert api_client.delete(f"/api/auth/api-keys/{key_payload['id']}").status_code == 204
    api_client.cookies.clear()
    assert api_client.get("/api/jobs", headers=key_headers).status_code == 401


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
    deleted_user_id = registered.json()["id"]
    deleted_job = Job(
        id="delete-account-results",
        emails=["delete-result@example.com"],
        worker_count=1,
        status="completed",
        owner_id=deleted_user_id,
        results=[{"email": "delete-result@example.com", "deliverable": True}],
    )
    job_store.add(deleted_job)
    with auth_store._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM job_results WHERE job_id=?", (deleted_job.id,)
        ).fetchone()[0] == 1
    assert deletion_account.delete("/api/auth/account").status_code == 204
    assert deletion_account.get("/api/auth/me").json() is None
    with auth_store._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM job_results WHERE job_id=?", (deleted_job.id,)
        ).fetchone()[0] == 0


legacy = load_legacy_module()
verifier = legacy.EmailVerifier()
config = verifier.get_consumer_fix_strategy("qq.com")
assert config["use_data_command"] is False
assert config["max_attempts"] == 1
assert config["max_mx_hosts"] == 1
assert legacy.smtp_gate_capacity("mx1.qq.com") == 1
assert verifier._handle_qq_response(250, b"OK", config, 0)[0] is True
assert verifier._handle_qq_response(550, b"Mailbox not found", config, 0)[0] is False
assert verifier._handle_qq_response(550, b"Access denied by policy", config, config["max_attempts"] - 1)[0] is False
assert verifier._handle_qq_response(553, b"Mailbox name not allowed", config, 0)[0] is False
assert verifier._handle_qq_response(554, b"Transaction failed", config, 0)[0] is False

missing_domain = legacy.EmailVerifier()
missing_domain.check_domain_exists = lambda _domain: False
missing = missing_domain.verify_email_comprehensive("person@missing-domain.test")
assert missing["deliverable"] is False
assert missing["checks"]["smtp"] is False

job_store.set_service_mode("draining")
try:
    with TestClient(app) as draining_client:
        response = draining_client.post("/api/verify/single", json={"email": "drain@example.com"})
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "60"
finally:
    job_store.set_service_mode("active")

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
