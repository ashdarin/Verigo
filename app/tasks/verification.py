from __future__ import annotations

import csv
import logging
import re
import secrets
import time
import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.config import settings
from app.core.legacy import create_verifier
from app.core.security import token_hash
from app.core.worker_lifecycle import TENCENT_QQ_TARGET, worker_lifecycle
from app.core.cloudshell_lifecycle import GMAIL_TARGET, cloudshell_lifecycle
from app.db.jobs import Job, job_store, utc_now


logger = logging.getLogger(__name__)
EMAIL_CHARACTERS = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+$")

CSV_FIELDS = [
    ("email", "邮箱地址"),
    ("deliverable", "可投递"),
    ("strategy", "验证策略"),
    ("verification_method", "验证方式"),
    ("smtp_result", "验证结果"),
    ("message", "说明"),
    ("timestamp", "验证时间"),
]

DELIVERABILITY_LABELS = {True: "可投递", False: "不可投递", None: "未知"}
METHOD_LABELS = {
    "standard": "邮箱服务器验证",
    "qq_rcpt": "邮箱服务器验证",
    "qq_avatar": "QQ 头像辅助证据",
    "microsoft_api": "微软账号验证",
    "catch-all_detected": "域名通用收件",
}


def normalize_result(result: dict[str, Any]) -> dict[str, Any]:
    """Return user-facing details without changing the verification verdict."""
    result = dict(result)
    detail = str(result.get("smtp_result") or result.get("message") or "")
    detail_lower = detail.lower()
    match = re.search(r"\b([245]\d{2})\b", detail)
    code = match.group(1) if match else None
    if "域名不存在" in detail:
        display_detail = "域名不存在"
    elif "mx" in detail_lower or "没有邮件服务器" in detail:
        display_detail = "没有邮箱服务器"
    elif "mail from" in detail_lower or "helo" in detail_lower:
        display_detail = f"{code} 邮箱服务器拒绝验证" if code else "邮箱服务器拒绝验证"
    elif code == "250":
        display_detail = "250 可投递"
    elif code == "550":
        display_detail = "550 不可投递"
    elif code and code.startswith("4"):
        display_detail = f"{code} 暂时无法确认"
    elif code and code.startswith("5"):
        display_detail = f"{code} 邮箱服务器拒绝验证"
    elif any(word in detail_lower for word in ("smtp", "连接", "超时", "connection", "timeout")):
        display_detail = "邮箱服务器暂时无法确认"
    else:
        display_detail = detail

    if display_detail:
        result["smtp_result"] = display_detail
    result["message"] = display_detail or result.get("message", "")
    result["verification_method"] = METHOD_LABELS.get(
        result.get("verification_method"), result.get("verification_method")
    )
    return result


def verification_filename(job: Job) -> str:
    verified_at = job.finished_at or job.started_at or job.created_at
    local_time = verified_at.astimezone(ZoneInfo("Asia/Shanghai"))
    return f"Verigo-邮箱验证-{local_time:%Y年%m月%d日-%H时%M分%S秒}.csv"


def clean_emails(values: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        email = str(value).strip()
        key = email.lower()
        if email and EMAIL_CHARACTERS.fullmatch(email) and key not in seen:
            seen.add(key)
            cleaned.append(email)
    return cleaned


def summarize(results: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(results),
        "valid": sum(item.get("valid") is True for item in results),
        "deliverable": sum(item.get("deliverable") is True for item in results),
        "undeliverable": sum(item.get("deliverable") is False for item in results),
        "unknown": sum(item.get("deliverable") is None and not item.get("skipped") for item in results),
        "catch_all": sum(item.get("domain_type") == "catch-all" for item in results),
    }


def write_csv(job: Job) -> None:
    settings.results_dir.mkdir(parents=True, exist_ok=True)
    path = settings.results_dir / verification_filename(job)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[label for _, label in CSV_FIELDS])
        writer.writeheader()
        for result in job.results:
            row = {label: result.get(key, "") for key, label in CSV_FIELDS}
            row["可投递"] = DELIVERABILITY_LABELS.get(result.get("deliverable"), "未知")
            writer.writerow(row)
    job.csv_path = path


class VerificationTasks:
    """The API submits durable jobs; dedicated worker services execute them."""

    def submit(
        self,
        emails: list[str],
        worker_count: int,
        owner_id: str | None = None,
        stop_on_deliverable: bool = False,
        job_id: str | None = None,
        execution_target: str = "local",
    ) -> Job:
        guest_token = None if owner_id else secrets.token_urlsafe(32)
        job = Job(
            id=job_id or uuid.uuid4().hex[:12],
            emails=clean_emails(emails),
            worker_count=worker_count,
            owner_id=owner_id,
            guest_token=guest_token,
            guest_token_hash=token_hash(guest_token) if guest_token else None,
            stop_on_deliverable=stop_on_deliverable,
            execution_target=execution_target,
        )
        job_store.add(job, max_active=settings.max_pending_jobs)
        if execution_target == TENCENT_QQ_TARGET:
            worker_lifecycle.notify_job_queued()
        elif execution_target == GMAIL_TARGET:
            cloudshell_lifecycle.notify_job_queued()
        return job

    def submit_partitioned(
        self,
        emails: list[str],
        worker_count: int,
        target_emails: dict[str, list[str]],
        owner_id: str | None = None,
        job_id: str | None = None,
    ) -> Job:
        """Create one visible task and target-specific internal child jobs."""
        all_emails = clean_emails(emails)
        partitions = [
            (target, clean_emails(partition_emails))
            for target, partition_emails in target_emails.items()
            if partition_emails
        ]
        partitioned_emails = [email for _, partition in partitions for email in partition]
        if (
            len(partitions) < 2
            or len(partitioned_emails) != len(all_emails)
            or {email.lower() for email in partitioned_emails}
            != {email.lower() for email in all_emails}
        ):
            raise ValueError("分流任务必须包含至少两个完整且互不重叠的执行分区")

        parent = Job(
            id=job_id or uuid.uuid4().hex[:12],
            emails=all_emails,
            worker_count=worker_count,
            status="running",
            started_at=utc_now(),
            owner_id=owner_id,
            guest_token=None if owner_id else secrets.token_urlsafe(32),
            stop_on_deliverable=False,
            execution_target="aggregate",
        )
        parent.guest_token_hash = (
            token_hash(parent.guest_token) if parent.guest_token else None
        )
        job_store.add(parent, max_active=settings.max_pending_jobs)

        for target, child_emails in partitions:
            child = Job(
                id=uuid.uuid4().hex[:12],
                emails=child_emails,
                worker_count=worker_count,
                stop_on_deliverable=False,
                execution_target=target,
                parent_id=parent.id,
            )
            job_store.add(child)
            if target == TENCENT_QQ_TARGET:
                worker_lifecycle.notify_job_queued()
            elif target == GMAIL_TARGET:
                cloudshell_lifecycle.notify_job_queued()
        return parent


def skipped_result(email: str, index: int) -> dict[str, Any]:
    return {
        "email": email,
        "timestamp": utc_now().strftime("%Y-%m-%d %H:%M:%S"),
        "valid": False,
        "deliverable": None,
        "domain_type": "-",
        "verification_method": "已停止",
        "smtp_result": "已找到可投递邮箱，未继续验证",
        "message": "已找到可投递邮箱，未继续验证",
        "original_index": index,
        "skipped": True,
    }


def sync_parent_job(job: Job) -> Job | None:
    """Refresh the visible mixed-domain task after a child update."""
    if not job.parent_id:
        return None
    parent = job_store.refresh_parent(job.parent_id)
    if parent is not None and parent.status == "completed":
        job_store.cache_results(parent.results)
        job_store.record_catch_all(parent)
        write_csv(parent)
        job_store.persist(parent)
    return parent


def verify_until_deliverable(
    job: Job, cached_by_email: dict[str, dict[str, Any]]
) -> dict[int, dict[str, Any]]:
    """Verify ordered candidates one by one so a confirmed match can stop the task."""
    by_index: dict[int, dict[str, Any]] = {}
    verifier: Any = None
    for index, email in enumerate(job.emails):
        if job_store.is_stopped(job.id):
            return by_index
        cached = cached_by_email.get(email.lower())
        if cached is not None:
            result = dict(cached)
            result["original_index"] = index
            result = normalize_result(result)
        else:
            if verifier is None:
                verifier = create_verifier(1)
                job.verifier = verifier
            batch_results = verifier.verify_batch_distributed(
                [email], num_processes=1, should_stop=lambda: job_store.is_stopped(job.id)
            )
            if job_store.is_stopped(job.id):
                return by_index
            result = normalize_result(dict(batch_results[0])) if batch_results else {
                "email": email,
                "deliverable": None,
                "original_index": index,
                "message": "验证未返回结果",
            }
            result["original_index"] = index
        by_index[index] = result
        if result.get("deliverable") is True:
            for remaining_index, remaining_email in enumerate(job.emails[index + 1 :], start=index + 1):
                by_index[remaining_index] = skipped_result(remaining_email, remaining_index)
            break
        job.results = [by_index[current] for current in sorted(by_index)]
        job_store.persist(job)
        job_store.heartbeat(job)
    return by_index


def run_job(job: Job) -> None:
    """Execute a claimed job and make incremental progress visible through SQLite."""
    job.status = "running"
    job.started_at = job.started_at or utc_now()
    job.heartbeat_at = utc_now()
    job_store.persist(job)
    try:
        if job_store.is_stopped(job.id):
            return
        cached_by_email = job_store.cached_results(job.emails)
        if job.stop_on_deliverable:
            by_index = verify_until_deliverable(job, cached_by_email)
            if job_store.is_stopped(job.id):
                return
            job.results = [by_index[index] for index in sorted(by_index)]
            job_store.cache_results(job.results)
            job_store.record_catch_all(job)
            job.finished_at = utc_now()
            write_csv(job)
            job.status = "completed"
            return

        by_index: dict[int, dict[str, Any]] = {}
        missing_emails: list[str] = []
        missing_indices: list[int] = []
        for index, email in enumerate(job.emails):
            cached = cached_by_email.get(email.lower())
            if cached is None:
                missing_indices.append(index)
                missing_emails.append(email)
                continue
            cached = dict(cached)
            cached["original_index"] = index
            by_index[index] = normalize_result(cached)

        job.results = [by_index[index] for index in sorted(by_index)]
        job_store.persist(job)
        job_store.heartbeat(job)

        if missing_emails:
            verifier = create_verifier(job.worker_count)
            job.verifier = verifier
            last_persist = 0.0

            def on_result(result: dict[str, Any]) -> None:
                nonlocal last_persist
                if job_store.is_stopped(job.id):
                    return
                result = dict(result)
                relative_index = int(result.get("original_index", 0))
                result["original_index"] = missing_indices[relative_index]
                by_index[result["original_index"]] = normalize_result(result)
                now = time.monotonic()
                if len(by_index) % 5 == 0 or now - last_persist >= 1.0:
                    job.results = [by_index[index] for index in sorted(by_index)]
                    job_store.persist(job)
                    job_store.heartbeat(job)
                    last_persist = now

            final_results = verifier.verify_batch_distributed(
                missing_emails,
                num_processes=job.worker_count,
                result_callback=on_result,
                should_stop=lambda: job_store.is_stopped(job.id),
            )
            if job_store.is_stopped(job.id):
                return
            for result in final_results:
                result = dict(result)
                relative_index = int(result.get("original_index", 0))
                result["original_index"] = missing_indices[relative_index]
                by_index[result["original_index"]] = normalize_result(result)

        job.results = [by_index[index] for index in sorted(by_index)]
        job_store.cache_results(job.results)
        job_store.record_catch_all(job)
        job.finished_at = utc_now()
        write_csv(job)
        job.status = "completed"
    except Exception as exc:
        logger.exception("Verification job %s failed", job.id)
        job.error = "任务执行失败，请稍后重新提交"
        job.status = "failed"
        job.finished_at = utc_now()
    finally:
        job.verifier = None
        job.heartbeat_at = utc_now()
        job_store.persist(job)
        sync_parent_job(job)


def job_progress(job: Job) -> tuple[int, int, float]:
    total = len(job.emails)
    if job.status == "completed":
        return total, total, 100.0
    if job.status == "queued":
        return 0, total, 0.0
    completed = min(len(job.results), total)
    percent = round((completed / total * 100) if total else 0, 1)
    return completed, total, percent


verification_tasks = VerificationTasks()
