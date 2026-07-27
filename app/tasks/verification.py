from __future__ import annotations

import csv
import logging
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.config import settings
from app.core.legacy import create_verifier
from app.core.provider_policy import YAHOO_UNSUPPORTED_MESSAGE, is_yahoo_email
from app.core.result_retry import (
    is_recipient_mailbox_full,
    is_smtp_greylisted,
    is_retryable_smtp_result,
    smtp_permanent_status,
    smtp_temporary_status,
)
from app.core.verification_outcome import (
    RETRY_NEVER,
    apply_outcome,
    ensure_outcome,
    retry_policy,
)
from app.core.security import token_hash
from app.core.worker_lifecycle import (
    DOMESTIC_CLOUDSTUDIO_TARGET,
    TENCENT_QQ_TARGET,
    domestic_worker_lifecycle,
    worker_lifecycle,
)
from app.core.cloudshell_lifecycle import GMAIL_TARGET, notify_cloudshell_job_queued
from app.db.jobs import Job, job_store, utc_now
from app.db.auth import auth_store


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
    "microsoft_api": "Outlook 账号验证",
    "catch-all_detected": "域名通用收件",
}


def normalize_result(result: dict[str, Any]) -> dict[str, Any]:
    """Normalize presentation and keep temporary SMTP failures inconclusive."""
    result = dict(result)
    ensure_outcome(result)
    detail = str(result.get("smtp_result") or result.get("message") or "")
    detail_lower = detail.lower()
    match = re.search(r"\b([245]\d{2})\b", detail)
    code = match.group(1) if match else None
    if result.get("failure_reason") == "domain_nxdomain":
        display_detail = "域名不存在"
    elif result.get("failure_reason") == "mx_missing":
        display_detail = "没有邮箱服务器"
    elif (
        result.get("verification_method") == "microsoft_api"
        or "微软接口" in detail
        or "接口a:" in detail_lower
        or "接口b:" in detail_lower
    ):
        if result.get("deliverable") is True:
            display_detail = "Outlook 邮箱已确认可投递"
        elif result.get("deliverable") is False:
            display_detail = "Outlook 邮箱不可投递"
        else:
            display_detail = "Outlook 邮箱暂时无法确认"
    elif is_recipient_mailbox_full({"smtp_result": detail}):
        result["smtp_raw_result"] = detail
        result["deliverable"] = False
        result["valid"] = False
        result["delivery_block_reason"] = "mailbox_full"
        checks = result.get("checks")
        if isinstance(checks, dict):
            checks["smtp"] = False
        display_detail = f"{code} 收件箱容量已满，当前无法接收邮件" if code else "收件箱容量已满，当前无法接收邮件"
    elif result.get("temporary_retries_exhausted"):
        result["deliverable"] = False
        result["valid"] = False
        checks = result.get("checks")
        if isinstance(checks, dict):
            checks["smtp"] = False
        display_detail = detail
    elif smtp_permanent_status({"smtp_result": detail}):
        result["deliverable"] = False
        result["valid"] = False
        checks = result.get("checks")
        if isinstance(checks, dict):
            checks["smtp"] = False
        display_detail = f"{code} 不可投递"
    elif code and code.startswith("4"):
        result["smtp_raw_result"] = detail
        result["deliverable"] = None
        result["valid"] = True
        checks = result.get("checks")
        if isinstance(checks, dict):
            checks["smtp"] = None
        result["temporary_smtp_code"] = code
        if is_smtp_greylisted({"smtp_result": detail}):
            display_detail = f"{code} 邮件服务器临时灰名单，正在重试"
        else:
            display_detail = f"{code} 邮件服务器暂时无法确认，正在重试"
    elif "mail from" in detail_lower or "helo" in detail_lower:
        display_detail = f"{code} 发送验证受限，不代表该邮箱不存在" if code else "发送验证受限，不代表该邮箱不存在"
    elif code == "250":
        display_detail = "250 可投递"
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
        for raw_result in job.results:
            result = normalize_result(raw_result)
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
        immediate_results: list[dict[str, Any]] | None = None,
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
        job.results = [waiting_result(email, index) for index, email in enumerate(job.emails)]
        if immediate_results is not None:
            job.results = immediate_results
            job.status = "completed"
            job.started_at = utc_now()
            job.finished_at = job.started_at
        job_store.add(job, max_active=settings.max_pending_jobs)
        if immediate_results is not None:
            job_store.cache_results(job.results)
            job_store.record_catch_all(job)
            write_csv(job)
            job_store.persist(job)
            return job
        if execution_target == TENCENT_QQ_TARGET:
            worker_lifecycle.notify_job_queued()
        elif execution_target == DOMESTIC_CLOUDSTUDIO_TARGET:
            domestic_worker_lifecycle.notify_job_queued()
        elif execution_target == GMAIL_TARGET:
            notify_cloudshell_job_queued()
        return job

    def submit_partitioned(
        self,
        emails: list[str],
        worker_count: int,
        target_emails: list[tuple[str, list[str], int]],
        owner_id: str | None = None,
        job_id: str | None = None,
        immediate_results_by_target: dict[str, list[dict[str, Any]]] | None = None,
    ) -> Job:
        """Create one visible task and target-specific internal child jobs."""
        all_emails = clean_emails(emails)
        partitions = [
            (target, clean_emails(partition_emails), child_worker_count)
            for target, partition_emails, child_worker_count in target_emails
            if partition_emails
        ]
        partitioned_emails = [email for _, partition, _ in partitions for email in partition]
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

        parent_positions: dict[str, list[int]] = {}
        for index, email in enumerate(parent.emails):
            parent_positions.setdefault(email.lower(), []).append(index)
        parent_offsets: dict[str, int] = {}

        for target, child_emails, child_worker_count in partitions:
            immediate_results = (immediate_results_by_target or {}).get(target)
            child = Job(
                id=uuid.uuid4().hex[:12],
                emails=child_emails,
                worker_count=child_worker_count,
                stop_on_deliverable=False,
                execution_target=target,
                parent_id=parent.id,
            )
            child.results = [
                waiting_result(email, index) for index, email in enumerate(child.emails)
            ]
            parent_indices: list[int] = []
            for email in child.emails:
                key = email.lower()
                offset = parent_offsets.get(key, 0)
                candidates = parent_positions.get(key, [])
                if offset >= len(candidates):
                    raise ValueError("Partition contains an email not present in its parent job")
                parent_indices.append(candidates[offset])
                parent_offsets[key] = offset + 1
            if immediate_results is not None:
                by_email = {
                    str(result.get("email", "")).lower(): dict(result)
                    for result in immediate_results
                }
                child.results = []
                for index, email in enumerate(child.emails):
                    result = dict(by_email.get(email.lower(), waiting_result(email, index)))
                    result["email"] = email
                    result["original_index"] = index
                    child.results.append(result)
                child.status = "completed"
                child.started_at = utc_now()
                child.finished_at = child.started_at
            job_store.add(child)
            job_store.link_child_results(child.id, parent.id, parent_indices)
            if immediate_results is not None:
                # The initial write happened before the link existed.
                job_store.upsert_results(child.id, child.results)
            if immediate_results is not None:
                continue
            if target == TENCENT_QQ_TARGET:
                worker_lifecycle.notify_job_queued()
            elif target == DOMESTIC_CLOUDSTUDIO_TARGET:
                domestic_worker_lifecycle.notify_job_queued()
            elif target == GMAIL_TARGET:
                notify_cloudshell_job_queued()
        job_store.refresh_parent(parent.id)
        return job_store.get(parent.id) or parent


def waiting_result(email: str, index: int) -> dict[str, Any]:
    """Make every submitted address visible before a worker returns its verdict."""
    return {
        "email": email,
        "timestamp": utc_now().strftime("%Y-%m-%d %H:%M:%S"),
        "valid": None,
        "deliverable": None,
        "domain_type": "-",
        "verification_method": "等待验证",
        "smtp_result": "等待验证",
        "message": "等待验证",
        "progress_state": "pending",
        "original_index": index,
    }


def yahoo_unsupported_result(email: str, index: int) -> dict[str, Any]:
    return {
        "email": email,
        "timestamp": utc_now().strftime("%Y-%m-%d %H:%M:%S"),
        "valid": False,
        "deliverable": None,
        "domain_type": "-",
        "verification_method": "不支持验证",
        "smtp_result": "不支持验证",
        "message": YAHOO_UNSUPPORTED_MESSAGE,
        "original_index": index,
        "skipped": True,
    }


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
        parent = job_store.get(parent.id) or parent
        job_store.cache_results(parent.results)
        job_store.record_catch_all(parent)
        write_csv(parent)
        job_store.persist(parent)
    return parent


def _notify_retry_target(job: Job) -> None:
    if job.execution_target == TENCENT_QQ_TARGET:
        worker_lifecycle.notify_job_queued()
    elif job.execution_target == DOMESTIC_CLOUDSTUDIO_TARGET:
        domestic_worker_lifecycle.notify_job_queued()
    elif job.execution_target == GMAIL_TARGET:
        notify_cloudshell_job_queued()


def enqueue_background_retry(
    parent: Job,
    source: Job,
    emails: list[str],
    attempt: int,
) -> None:
    """Queue a delayed recheck without changing the completed user task state."""
    if not emails:
        return
    if job_store.has_active_retry_child(parent.id):
        return
    retry_at = utc_now() + timedelta(seconds=settings.temporary_smtp_retry_seconds)
    email_keys = {email.lower() for email in emails}
    for result in parent.results:
        if str(result.get("email", "")).lower() not in email_keys:
            continue
        result["retry_state"] = "scheduled"
        result["retry_attempt"] = attempt
        result["retry_max_attempts"] = settings.temporary_smtp_immediate_retries
        result["retry_at"] = retry_at.isoformat()
    job_store.persist(parent)
    retry_job = Job(
        id=uuid.uuid4().hex[:12],
        emails=emails,
        worker_count=source.worker_count,
        owner_id=parent.owner_id,
        execution_target=source.execution_target,
        retry_parent_id=parent.id,
        deferred_retry_at=retry_at,
        temporary_retry_attempts=attempt,
    )
    job_store.add(retry_job)
    _notify_retry_target(retry_job)


def _clear_retry_metadata(result: dict[str, Any], state: str = "completed") -> None:
    """A finished recheck must not remain visible as an unresolved 4xx result."""
    result.pop("retry_at", None)
    result["retry_state"] = state
    result.pop("retry_attempt", None)
    result.pop("retry_max_attempts", None)


def finish_initial_job(job: Job) -> Job:
    """Complete the user task immediately and hand transient results to idle workers."""
    job.finished_at = utc_now()
    write_csv(job)
    job.error = None
    job.status = "completed"
    job_store.persist(job)
    visible = sync_parent_job(job) if job.parent_id else job
    visible = visible or job
    retry_emails = [
        str(result["email"])
        for result in visible.results
        if result.get("email")
        and is_retryable_smtp_result(result)
        and not result.get("greylist_retry_exhausted")
    ]
    enqueue_background_retry(visible, job, retry_emails, 1)
    if visible.status == "completed":
        job_store.cache_results(visible.results)
        job_store.record_catch_all(visible)
        write_csv(visible)
        job_store.persist(visible)
    return visible


def finish_background_retry(job: Job) -> Job | None:
    """Merge one deferred retry pass into its original, already-completed task."""
    if not job.retry_parent_id:
        return None
    parent = job_store.get(job.retry_parent_id)
    if parent is None:
        return None
    existing = {
        str(result.get("email", "")).lower(): dict(result)
        for result in parent.results if result.get("email")
    }
    next_retry: list[str] = []
    changed = 0
    for raw_result in job.results:
        result = normalize_result(raw_result)
        email = str(result.get("email", ""))
        previous = existing.get(email.lower(), {})
        if is_retryable_smtp_result(result):
            if job.temporary_retry_attempts >= settings.temporary_smtp_immediate_retries:
                finalize_temporary_smtp_results([result])
                _clear_retry_metadata(result)
            else:
                next_retry.append(email)
        else:
            _clear_retry_metadata(result)
        terminal = email not in next_retry
        if terminal and (
            result.get("deliverable") != previous.get("deliverable")
            or previous.get("retry_state") == "scheduled"
        ):
            changed += 1
            result["retry_updated"] = True
        existing[email.lower()] = result
    parent.results = [
        existing[email.lower()] for email in parent.emails if email.lower() in existing
    ]
    job_store.cache_results(parent.results)
    job_store.record_catch_all(parent)
    write_csv(parent)
    job_store.persist(parent)
    if next_retry:
        enqueue_background_retry(parent, job, next_retry, job.temporary_retry_attempts + 1)
    if changed and parent.owner_id:
        auth_store.create_notification(
            parent.owner_id,
            "verification_review",
            "任务复核结果已更新",
            f"{changed} 个邮箱的复核结果已更新",
        )
    return parent


def finish_background_retry_failure(job: Job, error: str) -> Job | None:
    """Keep an initial task complete when a deferred worker pass itself fails."""
    if not job.retry_parent_id:
        return None
    parent = job_store.get(job.retry_parent_id)
    if parent is None:
        return None

    affected = {email.lower() for email in job.emails}
    if job.temporary_retry_attempts < settings.temporary_smtp_immediate_retries:
        enqueue_background_retry(
            parent, job, job.emails, job.temporary_retry_attempts + 1
        )
        return parent

    changed = 0
    for result in parent.results:
        if str(result.get("email", "")).lower() not in affected:
            continue
        if not is_retryable_smtp_result(result):
            continue
        _clear_retry_metadata(result, "failed")
        result["deliverable"] = None
        result["valid"] = True
        result["smtp_result"] = "邮件服务器暂时无法复核，最终状态仍待确认"
        result["message"] = "邮件服务器暂时无法复核，最终状态仍待确认"
        result["retry_updated"] = True
        changed += 1
    job_store.cache_results(parent.results)
    write_csv(parent)
    job_store.persist(parent)
    if changed and parent.owner_id:
        auth_store.create_notification(
            parent.owner_id,
            "verification_review",
            "任务复核未完成",
            f"{changed} 个邮箱暂时无法完成复核，最终状态仍待确认。",
        )
    logger.warning("Deferred retry %s failed: %s", job.id, error)
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
        if is_yahoo_email(email):
            by_index[index] = yahoo_unsupported_result(email, index)
            continue
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


def finalize_temporary_smtp_results(results: list[dict[str, Any]]) -> None:
    """Finalize exhausted transient outcomes without turning DNS failures into false negatives."""
    for result in results:
        if retry_policy(result) == RETRY_NEVER:
            continue
        code = smtp_temporary_status(result)
        if not code:
            result["deliverable"] = None
            result["valid"] = True
            result["transient_retries_exhausted"] = True
            result["smtp_result"] = "验证基础设施暂时无法确认，复核次数已用尽"
            result["message"] = "验证基础设施暂时无法确认，复核次数已用尽"
            apply_outcome(
                result,
                stage=str(result.get("failure_stage") or "verification"),
                reason="transient_exhausted",
                retry_policy=RETRY_NEVER,
            )
            continue
        if is_recipient_mailbox_full(result):
            raw_detail = str(result.get("smtp_raw_result") or result.get("smtp_result") or "")
            result["smtp_raw_result"] = raw_detail
            result["deliverable"] = False
            result["valid"] = False
            result["delivery_block_reason"] = "mailbox_full"
            checks = result.get("checks")
            if isinstance(checks, dict):
                checks["smtp"] = False
            result.pop("retry_at", None)
            result["smtp_result"] = f"{code} 收件箱容量已满，当前无法接收邮件"
            result["message"] = f"{code} 收件箱容量已满，需要清理容量后才能接收邮件"
            continue
        if is_smtp_greylisted(result):
            result["deliverable"] = None
            result["valid"] = True
            result["greylist_retry_exhausted"] = True
            result["message"] = f"{code} 邮件服务器灰名单复核后仍无法确认"
            continue
        raw_detail = str(result.get("smtp_raw_result") or result.get("smtp_result") or "")
        result["smtp_raw_result"] = raw_detail
        result["deliverable"] = False
        result["valid"] = False
        checks = result.get("checks")
        if isinstance(checks, dict):
            checks["smtp"] = False
        result["temporary_retries_exhausted"] = True
        result["smtp_result"] = f"{code} 服务器连续 3 次未能确认，当前不可投递"
        result["message"] = f"{code} 邮件服务器连续 3 次未能确认，当前不可投递"


def schedule_remote_temporary_retry(job: Job) -> bool:
    """Give an older remote worker the same three retries at 60-second intervals."""
    temporary = [result for result in job.results if is_retryable_smtp_result(result)]
    if not temporary or all(
        int(result.get("temporary_smtp_retry_count", 0)) >= 3
        for result in temporary
    ):
        return False
    if job.temporary_retry_attempts >= settings.temporary_smtp_immediate_retries:
        return False

    job.temporary_retry_attempts += 1
    job.deferred_retry_at = utc_now() + timedelta(
        seconds=settings.temporary_smtp_retry_seconds
    )
    job.status = "queued"
    job.finished_at = None
    job.worker_id = None
    job.heartbeat_at = utc_now()
    job.error = (
        f"检测到 SMTP 临时响应，系统将在 60 秒内进行第 "
        f"{job.temporary_retry_attempts}/3 次重试"
    )
    for result in temporary:
        result["retry_at"] = job.deferred_retry_at.isoformat()
        result["retry_state"] = "scheduled"
        result["retry_attempt"] = job.temporary_retry_attempts
        result["retry_max_attempts"] = settings.temporary_smtp_immediate_retries
    return True


def schedule_greylist_retry(job: Job) -> bool:
    """Honor SMTP greylisting's published retry window without tying up a worker."""
    greylisted = [result for result in job.results if is_smtp_greylisted(result)]
    if not greylisted or job.temporary_retry_attempts >= settings.smtp_greylist_retry_max_attempts:
        return False

    job.temporary_retry_attempts += 1
    job.deferred_retry_at = utc_now() + timedelta(
        seconds=settings.smtp_greylist_retry_seconds
    )
    job.status = "queued"
    job.finished_at = None
    job.worker_id = None
    job.heartbeat_at = utc_now()
    job.error = f"SMTP 灰名单，正在等待下一次复核（第 {job.temporary_retry_attempts}/2 次）"
    for result in greylisted:
        result["retry_at"] = job.deferred_retry_at.isoformat()
    return True


def requeue_recent_single_temporary_jobs() -> int:
    """Repair completed single checks left in a temporary state by an older worker."""
    repaired = 0
    for job in job_store.recent_completed_single_jobs(utc_now() - timedelta(hours=24)):
        if job_store.has_active_retry_child(job.id):
            continue
        normalized = [normalize_result(result) for result in job.results]
        pending = [
            result for result in normalized
            if is_retryable_smtp_result(result)
            and not result.get("temporary_retries_exhausted")
            and not result.get("transient_retries_exhausted")
            and not result.get("greylist_retry_exhausted")
        ]
        if not pending:
            continue
        job.results = normalized
        job_store.persist(job)
        enqueue_background_retry(
            job,
            job,
            [str(result["email"]) for result in pending if result.get("email")],
            1,
        )
        repaired += 1
    if repaired:
        logger.info("Requeued %s completed single temporary SMTP jobs", repaired)
    return repaired


def apply_prospecting_receiver_protection(job: Job) -> Job | None:
    """Apply the prospecting-only safety policy after a completed worker lease."""
    from app.db.prospecting import prospecting_store

    decision = prospecting_store.apply_protection_outcomes(job.id, job.results)
    if decision is None:
        return None
    if decision["action"] == "stop":
        return job_store.stop_with_reason(job.id, str(decision["message"]))
    return job_store.defer_job(job.id, decision["resume_at"], str(decision["message"]))


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
            job.error = None
            job.status = "completed"
            return

        # A stopped task can be resumed in place. Keep its already reported
        # results and only schedule addresses that have not produced one.
        known_emails = {email.lower() for email in job.emails}
        by_index: dict[int, dict[str, Any]] = {
            int(result.get("original_index", index)): normalize_result(dict(result))
            for index, result in enumerate(job.results)
            if str(result.get("email", "")).lower() in known_emails
            and result.get("progress_state") not in {"pending", "verifying"}
        }
        leased_indices = set(job.pending_indices) if job.lease_id else None
        missing_emails: list[str] = []
        missing_indices: list[int] = []
        for index, email in enumerate(job.emails):
            if leased_indices is not None and index not in leased_indices:
                continue
            if index in by_index:
                continue
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
                if is_retryable_smtp_result(by_index[result["original_index"]]):
                    retry_at = utc_now() + timedelta(
                        seconds=settings.temporary_smtp_retry_seconds
                    )
                    by_index[result["original_index"]].update({
                        "retry_state": "scheduled",
                        "retry_attempt": 1,
                        "retry_max_attempts": settings.temporary_smtp_immediate_retries,
                        "retry_at": retry_at.isoformat(),
                    })
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
        if job.lease_id:
            job_store.persist(job)
            if not job_store.complete_lease(
                job.id, job.worker_id or "", job.lease_id
            ):
                return
            refreshed = job_store.get(job.id)
            if refreshed is None or job_store.is_stopped(job.id):
                return
            protected = apply_prospecting_receiver_protection(refreshed)
            if protected is not None:
                job = protected
                return
            overview = job_store.result_overview(job.id)
            if overview.settled < overview.total:
                job = refreshed
                return
            job = refreshed
            from app.db.prospecting import prospecting_store
            prospecting_store.finalize_run(job.id, job.results)
        if job.retry_parent_id:
            job.finished_at = utc_now()
            job.error = None
            job.status = "completed"
            job_store.persist(job)
            finish_background_retry(job)
        else:
            finish_initial_job(job)
    except Exception as exc:
        logger.exception("Verification job %s failed", job.id)
        if job.lease_id:
            job_store.abandon_lease(job.id, job.worker_id or "", job.lease_id)
            refreshed = job_store.get(job.id)
            if refreshed is not None:
                job = refreshed
            return
        job.error = "任务执行失败，请稍后重新提交"
        job.status = "failed"
        job.finished_at = utc_now()
        job_store.mark_unfinished_results_failed(job, job.error)
        if job.retry_parent_id:
            job_store.persist(job)
            finish_background_retry_failure(job, str(exc))
    finally:
        job.verifier = None
        job.heartbeat_at = utc_now()
        job_store.persist(job)
        sync_parent_job(job)


def job_progress(job: Job) -> tuple[int, int, float]:
    total = len(job.emails)
    if job.status == "completed":
        return total, total, 100.0
    completed = min(
        sum(
            result.get("progress_state") not in {"pending", "verifying"}
            for result in job.results
        ),
        total,
    )
    percent = round((completed / total * 100) if total else 0, 1)
    return completed, total, percent


verification_tasks = VerificationTasks()
