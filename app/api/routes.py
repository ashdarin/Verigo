from __future__ import annotations

import asyncio
import hmac
import json
import threading
import time
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Body, Depends, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse

from app.api.auth import optional_user, require_admin, require_user, request_network_hash
from app.api.schemas import (
    AdminCreditAdjustmentResponse,
    AdminCreditGrantRequest,
    CreateJobRequest,
    DiscoveryRequest,
    DiscoveryResponse,
    ImportResponse,
    JobResponse,
    NotificationListResponse,
    PaymentOrderRequest,
    PaymentOrderResponse,
    ResultsResponse,
    SingleVerificationRequest,
    WorkerFailureRequest,
    WorkerResultsRequest,
)
from app.config import settings
from app.core.imports import extract_emails
from app.core.discovery import candidate_emails
from app.core.security import token_hash
from app.core.worker_lifecycle import (
    DOMESTIC_CLOUDSTUDIO_TARGET,
    TENCENT_QQ_TARGET,
    domestic_worker_lifecycle,
    worker_lifecycle,
)
from app.core.cloudshell_lifecycle import (
    GMAIL_TARGET,
    cloudshell_lifecycle,
    notify_cloudshell_job_queued,
)
from app.core.provider_policy import (
    YAHOO_UNSUPPORTED_MESSAGE,
    is_qq_email,
    is_yahoo_email,
    is_yahoo_domain,
    yahoo_addresses,
)
from app.db.auth import User, auth_store
from app.db.jobs import Job, job_store, utc_now
from app.db.metrics import metrics_store
from app.tasks.verification import (
    clean_emails,
    job_progress,
    normalize_result,
    finalize_temporary_smtp_results,
    finish_background_retry,
    finish_background_retry_failure,
    finish_initial_job,
    summarize,
    sync_parent_job,
    verification_filename,
    verification_tasks,
    write_csv,
    yahoo_unsupported_result,
)


router = APIRouter(prefix="/api")
DOMESTIC_EMAIL_DOMAINS = frozenset({
    "qq.com", "vip.qq.com", "foxmail.com", "163.com", "126.com", "yeah.net",
    "sina.com", "sina.cn", "sohu.com", "aliyun.com", "aliyun.cn", "139.com",
    "189.cn", "wo.cn", "21cn.com", "tom.com",
})
FOREIGN_EMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "icloud.com", "me.com", "mac.com", "proton.me", "protonmail.com", "pm.me",
    "aol.com", "yandex.com", "yandex.ru", "zoho.com",
})
REMOTE_WORKERS = {
    "tencent-qq": "tencent_qq",
    "cloudstudio-domestic": DOMESTIC_CLOUDSTUDIO_TARGET,
    "gmail": "gmail",
}
REMOTE_RESULT_BATCH_SIZE = 25
_remote_result_batches: dict[str, list[dict[str, object]]] = {}
_remote_result_batches_lock = threading.Lock()


def buffer_remote_results(
    job_id: str,
    worker_id: str,
    results: list[dict[str, object]],
    *,
    force: bool = False,
) -> list[dict[str, object]]:
    """Return a durable-sized batch without writing a full job JSON per result."""
    # The endpoint validates the active lease before buffering, so a job has
    # exactly one valid result stream regardless of the worker process ID.
    key = job_id
    with _remote_result_batches_lock:
        batch = _remote_result_batches.setdefault(key, [])
        batch.extend(dict(result) for result in results)
        if not force and len(batch) < REMOTE_RESULT_BATCH_SIZE:
            return []
        return _remote_result_batches.pop(key)


def discard_buffered_remote_results(job_id: str) -> None:
    with _remote_result_batches_lock:
        _remote_result_batches.pop(job_id, None)


def remote_worker_label(execution_target: str) -> str:
    return {
        "tencent_qq": "腾讯 QQ 验证节点",
        DOMESTIC_CLOUDSTUDIO_TARGET: "国内邮箱 Cloud Studio 验证节点",
        GMAIL_TARGET: "Google Cloud Shell 验证节点",
    }.get(execution_target, "远程验证节点")


def remote_worker_count(execution_target: str, requested_count: int) -> int:
    """Apply the target-specific concurrency cap before a remote job is queued."""
    limit = (
        settings.cloudshell_worker_max_workers
        if execution_target == "gmail"
        else settings.cloudstudio_worker_max_workers
    )
    return max(1, min(requested_count, limit))


def require_job(job_id: str, *, include_results: bool = True) -> Job:
    job = job_store.get(job_id, include_results=include_results)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或服务已重启")
    return job


def tencent_qq_target(emails: list[str], owner_email: str | None) -> str:
    if not qq_worker_allowed(owner_email) or not emails:
        return "local"
    if any(is_qq_email(email) for email in emails):
        return "tencent_qq"
    domains = {email.rsplit("@", 1)[-1].lower() for email in emails if "@" in email}
    return "tencent_qq" if domains and all(is_domestic_email_domain(domain) for domain in domains) else "local"


def gmail_target(emails: list[str], owner_email: str | None) -> str:
    if not gmail_worker_allowed(owner_email) or not emails:
        return "local"
    domains = {email.rsplit("@", 1)[-1].lower() for email in emails if "@" in email}
    return "gmail" if domains and all(is_foreign_email_domain(domain) for domain in domains) else "local"


def is_domestic_email_domain(domain: str) -> bool:
    return domain in DOMESTIC_EMAIL_DOMAINS or domain.endswith(".cn")


def is_foreign_email_domain(domain: str) -> bool:
    if domain in FOREIGN_EMAIL_DOMAINS:
        return True
    suffix = domain.rsplit(".", 1)[-1] if "." in domain else ""
    return len(suffix) == 2 and suffix != "cn"


def qq_worker_allowed(owner_email: str | None) -> bool:
    allowed = settings.tencent_qq_worker_allowed_emails
    return bool(
        settings.tencent_qq_worker_enabled
        and ("*" in allowed or (owner_email and owner_email.lower() in allowed))
    )


def gmail_worker_allowed(owner_email: str | None) -> bool:
    allowed = settings.gmail_worker_allowed_emails
    return bool(
        settings.gmail_worker_enabled
        and ("*" in allowed or (owner_email and owner_email.lower() in allowed))
    )


def domestic_worker_allowed(owner_email: str | None) -> bool:
    allowed = settings.tencent_qq_worker_allowed_emails
    return bool(
        settings.cloudstudio_domestic_worker_enabled
        and ("*" in allowed or (owner_email and owner_email.lower() in allowed))
    )


def email_execution_target(email: str, owner_email: str | None) -> str:
    """Return the configured worker target for one address."""
    if is_qq_email(email):
        return "tencent_qq"
    domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    if is_domestic_email_domain(domain) and domestic_worker_allowed(owner_email):
        return DOMESTIC_CLOUDSTUDIO_TARGET
    if is_domestic_email_domain(domain) and qq_worker_allowed(owner_email):
        return "tencent_qq"
    if is_foreign_email_domain(domain) and gmail_worker_allowed(owner_email):
        return "gmail"
    return "local"


def partition_target_emails(
    targets: dict[tuple[str, int], list[str]],
) -> list[tuple[str, list[str], int]]:
    """Keep remote completion requests below their maximum supported payload."""
    partitions: list[tuple[str, list[str], int]] = []
    remote_targets = {"tencent_qq", DOMESTIC_CLOUDSTUDIO_TARGET, "gmail"}
    for (target, child_worker_count), target_emails in targets.items():
        if target not in remote_targets:
            chunk_size = len(target_emails)
        elif target == "tencent_qq":
            # QQ remains on its single serial worker regardless of task size.
            chunk_size = settings.remote_worker_max_emails_per_job
        else:
            # Worker capacity is discovered at claim time. Do not encode a fixed
            # number of Cloud Shell accounts into submission-time partitions.
            chunk_size = settings.remote_worker_max_emails_per_job
        for start in range(0, len(target_emails), chunk_size):
            partitions.append(
                (target, target_emails[start : start + chunk_size], child_worker_count)
            )
    return partitions


def submit_routed_job(
    emails: list[str],
    worker_count: int,
    owner_id: str | None,
    owner_email: str | None,
    stop_on_deliverable: bool = False,
    job_id: str | None = None,
) -> Job:
    targets: dict[tuple[str, int], list[str]] = {}
    for email in emails:
        target = (
            "unsupported"
            if is_yahoo_email(email)
            else email_execution_target(email, owner_email)
        )
        # QQ verification stays on Cloud Studio and is intentionally serial.
        # Cloud Studio otherwise retains its existing cap; Cloud Shell can use
        # eight processes when the user chooses Fastest mode.
        child_worker_count = (
            1
            if is_qq_email(email)
            else remote_worker_count(target, worker_count)
            if target in {"tencent_qq", DOMESTIC_CLOUDSTUDIO_TARGET, "gmail"}
            else worker_count
        )
        targets.setdefault((target, child_worker_count), []).append(email)

    immediate_results = {
        "unsupported": [
            yahoo_unsupported_result(email, index)
            for index, email in enumerate(
                targets.get(("unsupported", worker_count), [])
            )
        ]
    }

    partitions = partition_target_emails(targets)
    # Candidate discovery must preserve its input order and stop as soon as it
    # confirms a deliverable address, so it cannot be processed concurrently.
    if not stop_on_deliverable and len(partitions) > 1:
        return verification_tasks.submit_partitioned(
            emails,
            worker_count,
            partitions,
            owner_id=owner_id,
            job_id=job_id,
            immediate_results_by_target=immediate_results,
        )

    if stop_on_deliverable and len(partitions) > 1:
        # This mode must stop globally after the first deliverable result, which
        # cannot be preserved across concurrent remote child jobs.
        return verification_tasks.submit(
            emails,
            worker_count,
            owner_id=owner_id,
            stop_on_deliverable=True,
            job_id=job_id,
            execution_target="local",
        )

    execution_target, child_worker_count = (
        next(iter(targets)) if len(targets) == 1 else ("local", worker_count)
    )
    return verification_tasks.submit(
        emails,
        child_worker_count,
        owner_id=owner_id,
        stop_on_deliverable=stop_on_deliverable,
        job_id=job_id,
        execution_target=execution_target,
        immediate_results=immediate_results.get(execution_target),
    )


def submit_stopped_job_continuation(job: Job) -> Job:
    """Continue a stopped task without changing its user-visible task ID."""
    resumed, queued_jobs = job_store.resume(job.id)
    if resumed is None:
        raise RuntimeError("任务不存在")
    if not queued_jobs:
        raise ValueError("该任务没有可继续验证的邮箱")

    for queued_job in queued_jobs:
        if queued_job.execution_target == TENCENT_QQ_TARGET:
            worker_lifecycle.notify_job_queued()
        elif queued_job.execution_target == DOMESTIC_CLOUDSTUDIO_TARGET:
            domestic_worker_lifecycle.notify_job_queued()
        elif queued_job.execution_target == GMAIL_TARGET:
            notify_cloudshell_job_queued()
    return resumed


def require_remote_worker(worker_target: str, token: str | None) -> str:
    execution_target = REMOTE_WORKERS.get(worker_target)
    if execution_target is None:
        raise HTTPException(status_code=404, detail="未知远程验证节点")
    configured_token = (
        settings.tencent_qq_worker_token
        if execution_target == "tencent_qq"
        else settings.cloudstudio_domestic_worker_token
        if execution_target == DOMESTIC_CLOUDSTUDIO_TARGET
        else settings.gmail_worker_token
    )
    if not configured_token:
        raise HTTPException(status_code=503, detail="远程验证节点尚未配置")
    if not token or not hmac.compare_digest(token, configured_token):
        raise HTTPException(status_code=401, detail="远程验证节点认证失败")
    return execution_target


def require_remote_job(job_id: str, worker_id: str, execution_target: str, lease_id: str | None = None) -> Job:
    job = require_job(job_id, include_results=False)
    valid_lease = lease_id and job_store.lease_valid(job_id, worker_id, lease_id)
    if job.execution_target != execution_target or (not valid_lease and job.worker_id != worker_id):
        raise HTTPException(status_code=409, detail="远程验证节点任务租约无效")
    if execution_target == "tencent_qq":
        worker_lifecycle.record_worker_seen(worker_id)
    elif execution_target == DOMESTIC_CLOUDSTUDIO_TARGET:
        domestic_worker_lifecycle.record_worker_seen(worker_id)
    else:
        cloudshell_lifecycle.record_worker_seen(worker_id)
    return job


def merge_worker_results(job: Job, results: list[dict[str, object]]) -> Job:
    normalized: list[dict[str, object]] = []
    for raw_result in results:
        result = dict(raw_result)
        try:
            index = int(result.get("original_index", -1))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="腾讯节点结果缺少有效序号") from exc
        if index < 0 or index >= len(job.emails):
            raise HTTPException(status_code=422, detail="腾讯节点结果序号超出任务范围")
        if str(result.get("email", "")).lower() != job.emails[index].lower():
            raise HTTPException(status_code=422, detail="腾讯节点结果邮箱与任务不匹配")
        result["original_index"] = index
        result = normalize_result(result)
        result["progress_state"] = "completed"
        normalized.append(result)
    # Durable result rows are independent of the job metadata row. A repeated
    # callback cannot overwrite a terminal result with a waiting state.
    job_store.upsert_results(job.id, normalized)
    return job


def require_job_access(job: Job, user: User | None, guest_token: str | None) -> Job:
    if job.owner_id is not None:
        if user is None or user.id != job.owner_id:
            raise HTTPException(status_code=404, detail="任务不存在")
        return job
    if (
        not guest_token
        or not job.guest_token_hash
        or not hmac.compare_digest(token_hash(guest_token), job.guest_token_hash)
    ):
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


def serialize_job(job: Job) -> JobResponse:
    completed, total, progress = job_progress(job)
    is_done = job.status in {"completed", "stopped"}
    normalized_results = [normalize_result(result) for result in job.results]
    retry_at = job.deferred_retry_at
    result_retries = []
    for result in job.results:
        if result.get("retry_state") != "scheduled" or not result.get("retry_at"):
            continue
        try:
            result_retries.append(datetime.fromisoformat(str(result["retry_at"])))
        except ValueError:
            continue
    if result_retries:
        retry_at = min([retry_at, *result_retries] if retry_at else result_retries)
    if job.execution_target == "aggregate":
        child_retries = [
            child.deferred_retry_at for child in job_store.children(job.id)
            if child.deferred_retry_at is not None
        ]
        if child_retries:
            retry_at = min(child_retries)
    return JobResponse(
        id=job.id,
        status=job.status,
        worker_count=job.worker_count,
        completed=completed,
        total=total,
        progress=progress,
        created_at=job.created_at.isoformat(),
        started_at=job.started_at.isoformat() if job.started_at else None,
        finished_at=job.finished_at.isoformat() if job.finished_at else None,
        error=job.error,
        summary=summarize(normalized_results),
        download_url=f"/api/jobs/{job.id}/download" if is_done else None,
        download_name=verification_filename(job) if is_done else None,
        queue_position=job_store.queue_position(job.id),
        retry_at=retry_at.isoformat() if retry_at else None,
        stop_on_deliverable=job.stop_on_deliverable,
        qq_slow=any(is_qq_email(email) for email in job.emails),
        review_updated=any(result.get("retry_updated") for result in job.results),
        access_token=job.guest_token,
    )


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/workers/cloudstudio/probe")
def cloudstudio_probe(
    request: Request,
    token: Annotated[str | None, Header(alias="X-Verigo-CloudStudio-Probe-Token")] = None,
    workspace_key: Annotated[str | None, Header(alias="X-Verigo-CloudStudio-Workspace-Key")] = None,
) -> dict[str, str]:
    configured_token = settings.cloudstudio_probe_token
    if not configured_token:
        raise HTTPException(status_code=503, detail="CloudStudio 连通性探针尚未配置")
    if not token or not hmac.compare_digest(token, configured_token):
        raise HTTPException(status_code=401, detail="CloudStudio 连通性探针认证失败")
    if not workspace_key or len(workspace_key) > 64:
        raise HTTPException(status_code=422, detail="CloudStudio 工作空间标识无效")

    forwarded_for = request.headers.get("x-forwarded-for", "")
    source = forwarded_for.split(",", 1)[0].strip() or (
        request.client.host if request.client else "unknown"
    )
    print(f"CloudStudio probe accepted: workspace={workspace_key} source={source}", flush=True)
    return {"status": "accepted", "workspace_key": workspace_key}


@router.post("/workers/{worker_target}/claim")
async def claim_tencent_qq_job(
    worker_target: str,
    token: Annotated[str | None, Header(alias="X-Verigo-Worker-Token")] = None,
    worker_id: Annotated[str | None, Header(alias="X-Verigo-Worker-Id")] = None,
    wait_seconds: int = Query(default=20, ge=0, le=25),
) -> dict[str, object]:
    execution_target = require_remote_worker(worker_target, token)
    worker_name = (worker_id or "").strip()
    if not worker_name or len(worker_name) > 128:
        raise HTTPException(status_code=422, detail="腾讯 QQ 验证节点标识无效")
    if execution_target == "tencent_qq":
        worker_lifecycle.record_worker_seen(worker_name)
    elif execution_target == DOMESTIC_CLOUDSTUDIO_TARGET:
        domestic_worker_lifecycle.record_worker_seen(worker_name)
    else:
        cloudshell_lifecycle.record_worker_seen(worker_name)
    deadline = time.monotonic() + wait_seconds
    while True:
        job = job_store.claim_remote_lease(
            worker_name, execution_target, capacity=1,
            shard_size=min(100, settings.remote_worker_max_emails_per_job),
        )
        if job is not None:
            sync_parent_job(job)
            return {
                "job": {
                    "id": job.id,
                    "items": [
                        {"email": job.emails[index], "original_index": index}
                        for index in job.pending_indices
                    ],
                    "pending_indices": job.pending_indices,
                    "lease_id": job.lease_id,
                    "worker_count": remote_worker_count(
                        execution_target, job.worker_count
                    ),
                    "stop_on_deliverable": job.stop_on_deliverable,
                }
            }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"job": None}
        await asyncio.sleep(min(0.25, remaining))


@router.post("/workers/{worker_target}/jobs/{job_id}/heartbeat")
def heartbeat_tencent_qq_job(
    worker_target: str,
    job_id: str,
    token: Annotated[str | None, Header(alias="X-Verigo-Worker-Token")] = None,
    worker_id: Annotated[str | None, Header(alias="X-Verigo-Worker-Id")] = None,
    lease_id: str | None = Query(default=None, min_length=8, max_length=64),
) -> dict[str, object]:
    execution_target = require_remote_worker(worker_target, token)
    job = require_job(job_id, include_results=False)
    if job.execution_target != execution_target:
        raise HTTPException(status_code=409, detail="不是腾讯 QQ 验证节点任务")
    if job.status == "stopped":
        discard_buffered_remote_results(job.id)
        return {"status": "stopped", "stop_requested": True}
    worker_name = (worker_id or "").strip()
    job = require_remote_job(job_id, worker_name, execution_target, lease_id)
    if lease_id:
        job_store.heartbeat_lease(job.id, worker_name, lease_id)
    else:
        job_store.heartbeat(job)
    return {"status": job.status, "stop_requested": False}


@router.post("/workers/{worker_target}/jobs/{job_id}/results")
def report_tencent_qq_results(
    worker_target: str,
    job_id: str,
    payload: WorkerResultsRequest,
    token: Annotated[str | None, Header(alias="X-Verigo-Worker-Token")] = None,
    worker_id: Annotated[str | None, Header(alias="X-Verigo-Worker-Id")] = None,
) -> dict[str, object]:
    execution_target = require_remote_worker(worker_target, token)
    job = require_job(job_id, include_results=False)
    if job.execution_target != execution_target:
        raise HTTPException(status_code=409, detail="不是腾讯 QQ 验证节点任务")
    if job.status == "stopped":
        return {"status": "stopped", "stop_requested": True}
    worker_name = (worker_id or "").strip()
    job = require_remote_job(job_id, worker_name, execution_target, payload.lease_id)
    results_to_persist = buffer_remote_results(job.id, worker_name, payload.results)
    if results_to_persist:
        merge_worker_results(job, results_to_persist)
        sync_parent_job(job)
    if payload.lease_id:
        job_store.heartbeat_lease(job.id, worker_name, payload.lease_id)
    else:
        job_store.heartbeat(job)
    return {
        "status": job.status,
        "stop_requested": False,
        "accepted": len(payload.results),
        "persisted": len(results_to_persist),
    }


@router.post("/workers/{worker_target}/jobs/{job_id}/complete", response_model=JobResponse)
def complete_tencent_qq_job(
    worker_target: str,
    job_id: str,
    payload: WorkerResultsRequest,
    token: Annotated[str | None, Header(alias="X-Verigo-Worker-Token")] = None,
    worker_id: Annotated[str | None, Header(alias="X-Verigo-Worker-Id")] = None,
) -> JobResponse:
    execution_target = require_remote_worker(worker_target, token)
    job = require_job(job_id, include_results=False)
    if job.execution_target != execution_target:
        raise HTTPException(status_code=409, detail="不是腾讯 QQ 验证节点任务")
    if job.status == "stopped":
        discard_buffered_remote_results(job.id)
        return serialize_job(job)
    worker_name = (worker_id or "").strip()
    job = require_remote_job(job_id, worker_name, execution_target, payload.lease_id)
    merge_worker_results(
        job,
        buffer_remote_results(job.id, worker_name, payload.results, force=True),
    )
    if payload.lease_id:
        job_store.complete_lease(job.id, worker_name, payload.lease_id)
    if job_store.pending_count(job.id):
        sync_parent_job(job)
        return serialize_job(job_store.get(job.id) or job)
    job = job_store.get(job.id) or job
    if job.retry_parent_id:
        job.finished_at = utc_now()
        job.error = None
        job.status = "completed"
        job_store.persist(job)
        finish_background_retry(job)
    else:
        finish_initial_job(job)
    return serialize_job(job)


@router.post("/workers/{worker_target}/jobs/{job_id}/fail", response_model=JobResponse)
def fail_tencent_qq_job(
    worker_target: str,
    job_id: str,
    payload: WorkerFailureRequest,
    token: Annotated[str | None, Header(alias="X-Verigo-Worker-Token")] = None,
    worker_id: Annotated[str | None, Header(alias="X-Verigo-Worker-Id")] = None,
) -> JobResponse:
    execution_target = require_remote_worker(worker_target, token)
    job = require_job(job_id)
    if job.execution_target != execution_target:
        raise HTTPException(status_code=409, detail="不是腾讯 QQ 验证节点任务")
    if job.status == "stopped":
        return serialize_job(job)
    job = require_remote_job(job_id, (worker_id or "").strip(), execution_target)
    job.error = f"{remote_worker_label(execution_target)}失败: {payload.error}"
    job.status = "failed"
    job.finished_at = utc_now()
    job_store.mark_unfinished_results_failed(job, job.error)
    if job.retry_parent_id:
        finish_background_retry_failure(job, payload.error)
    sync_parent_job(job)
    return serialize_job(job)


@router.post("/analytics/engage", status_code=204)
def record_analytics_engagement(
    request: Request,
    seconds: int = Body(default=0, embed=True, ge=0, le=1800),
) -> None:
    session_id = request.cookies.get("verigo_analytics")
    if session_id:
        metrics_store.record_engagement(session_id, seconds)


@router.get("/admin/metrics")
def admin_metrics(_: Annotated[User, Depends(require_admin)]) -> dict[str, object]:
    return metrics_store.snapshot()


@router.post("/admin/credits/grant", response_model=AdminCreditAdjustmentResponse)
def grant_admin_credits(
    payload: AdminCreditGrantRequest,
    admin: Annotated[User, Depends(require_admin)],
) -> AdminCreditAdjustmentResponse:
    try:
        adjustment = auth_store.adjust_paid_credits(
            payload.email, payload.credits, admin.id, payload.note, payload.amount_fen
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return AdminCreditAdjustmentResponse(
        email=adjustment.user.email or payload.email,
        delta=adjustment.delta,
        credits=adjustment.user.credits,
        paid_credits=adjustment.user.paid_credits,
        reference=adjustment.reference,
        created_at=adjustment.created_at,
    )


@router.post("/admin/credits/deduct", response_model=AdminCreditAdjustmentResponse)
def deduct_admin_credits(
    payload: AdminCreditGrantRequest,
    admin: Annotated[User, Depends(require_admin)],
) -> AdminCreditAdjustmentResponse:
    try:
        adjustment = auth_store.adjust_paid_credits(
            payload.email, -payload.credits, admin.id, payload.note, payload.amount_fen
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return AdminCreditAdjustmentResponse(
        email=adjustment.user.email or payload.email,
        delta=adjustment.delta,
        credits=adjustment.user.credits,
        paid_credits=adjustment.user.paid_credits,
        reference=adjustment.reference,
        created_at=adjustment.created_at,
    )

@router.get("/admin/accounts")
def admin_account_snapshot(
    email: str = Query(min_length=3, max_length=254),
    _: Annotated[User, Depends(require_admin)] = None,
) -> dict[str, object]:
    try:
        return auth_store.admin_account_snapshot(email)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

@router.get("/admin/accounts/list")
def admin_accounts(offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=10, le=100), _: Annotated[User, Depends(require_admin)] = None) -> dict[str, object]:
    items, total, summary = auth_store.list_admin_accounts(offset, limit)
    return {
        "items": items, "total": total, "summary": summary,
        "offset": offset, "limit": limit,
    }

@router.get("/admin/feature-usage")
def admin_feature_usage(_: Annotated[User, Depends(require_admin)]) -> dict[str, object]:
    return metrics_store.feature_usage()


@router.get("/notifications", response_model=NotificationListResponse)
def list_notifications(user: Annotated[User, Depends(require_user)]) -> NotificationListResponse:
    items, unread_count = auth_store.list_notifications(user.id)
    return NotificationListResponse(items=items, unread_count=unread_count)


@router.post("/notifications/read", status_code=204)
def mark_notifications_read(user: Annotated[User, Depends(require_user)]) -> None:
    auth_store.mark_notifications_read(user.id)

@router.get("/wallet")
def wallet_snapshot(user: Annotated[User, Depends(require_user)]) -> dict[str, object]:
    return auth_store.wallet_snapshot(user.id)


@router.post("/discovery/candidates", response_model=DiscoveryResponse)
def discovery_candidates(
    payload: DiscoveryRequest,
    _: Annotated[User, Depends(require_user)],
) -> DiscoveryResponse:
    if is_yahoo_domain(payload.domain):
        raise HTTPException(status_code=422, detail=YAHOO_UNSUPPORTED_MESSAGE)
    try:
        candidates = candidate_emails(payload.first_name, payload.last_name, payload.domain)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return DiscoveryResponse(candidates=candidates)


@router.post("/discovery/verify", response_model=JobResponse, status_code=202)
def verify_discovery_candidates(
    payload: DiscoveryRequest,
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> JobResponse:
    if not user.email_verified:
        raise HTTPException(status_code=403, detail="请先验证注册邮箱")
    try:
        candidates = candidate_emails(payload.first_name, payload.last_name, payload.domain)
        job = submit_routed_job(
            candidates,
            4,
            owner_id=user.id,
            stop_on_deliverable=True,
            job_id=uuid.uuid4().hex[:12],
            owner_email=user.email,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    metrics_store.record_conversion(request.cookies.get("verigo_analytics"), "free")
    return serialize_job(job)


@router.post("/jobs", response_model=JobResponse, status_code=202)
def create_job(
    payload: CreateJobRequest,
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> JobResponse:
    emails = clean_emails(payload.emails)
    if not emails:
        raise HTTPException(status_code=422, detail="邮箱包含空格、非 ASCII 或非法字符")
    job_limit = settings.max_emails_per_job
    if job_limit > 0 and len(emails) > job_limit:
        raise HTTPException(status_code=422, detail=f"单次最多 {job_limit} 个邮箱")
    job_id = uuid.uuid4().hex[:12]
    charge_reference = f"verification:{job_id}"
    charged_count = len(emails) - len(yahoo_addresses(emails))
    try:
        if charged_count:
            auth_store.consume_credits(user.id, charged_count, charge_reference)
        job = submit_routed_job(
            emails,
            payload.worker_count,
            owner_id=user.id,
            owner_email=user.email,
            stop_on_deliverable=payload.stop_on_deliverable,
            job_id=job_id,
        )
    except RuntimeError as exc:
        if charged_count:
            auth_store.refund_credits(user.id, charged_count, charge_reference)
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    metrics_store.record_conversion(request.cookies.get("verigo_analytics"), "batch")
    return serialize_job(job)


@router.post("/verify/single", response_model=JobResponse, status_code=202)
def verify_single_email(
    payload: SingleVerificationRequest,
    request: Request,
    user: Annotated[User | None, Depends(optional_user)],
) -> JobResponse:
    emails = clean_emails([payload.email])
    if len(emails) != 1:
        raise HTTPException(status_code=422, detail="请输入有效的邮箱地址")
    yahoo_only = bool(yahoo_addresses(emails))
    try:
        if not yahoo_only:
            metrics_store.reserve_free_single(
                request_network_hash(request), settings.anonymous_free_single_daily_limit
            )
        job = submit_routed_job(
            emails,
            1,
            owner_id=user.id if user else None,
            owner_email=user.email if user else None,
            job_id=uuid.uuid4().hex[:12],
        )
    except RuntimeError as exc:
        if not yahoo_only:
            metrics_store.release_free_single(request_network_hash(request))
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    metrics_store.record_conversion(request.cookies.get("verigo_analytics"), "free")
    return serialize_job(job)


@router.post("/billing/orders", response_model=PaymentOrderResponse, status_code=201)
def create_payment_order(
    payload: PaymentOrderRequest, user: Annotated[User, Depends(require_user)]
) -> PaymentOrderResponse:
    order = auth_store.create_payment_order(user.id, payload.packages)
    return PaymentOrderResponse(**order)


@router.get("/jobs", response_model=list[JobResponse])
def list_jobs(
    user: Annotated[User, Depends(require_user)],
    limit: int = Query(default=10, ge=1, le=50),
) -> list[JobResponse]:
    return [serialize_job(job) for job in job_store.list_recent(user.id, limit)]


@router.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(
    job_id: str,
    user: Annotated[User | None, Depends(optional_user)],
    guest_token: Annotated[str | None, Header(alias="X-Job-Token")] = None,
) -> JobResponse:
    return serialize_job(require_job_access(require_job(job_id), user, guest_token))


@router.post("/jobs/{job_id}/reviewed", status_code=204)
def mark_job_reviewed(
    job_id: str,
    user: Annotated[User | None, Depends(optional_user)],
    guest_token: Annotated[str | None, Header(alias="X-Job-Token")] = None,
) -> None:
    job = require_job_access(require_job(job_id), user, guest_token)
    for result in job.results:
        result.pop("retry_updated", None)
    job_store.persist(job)


@router.post("/jobs/{job_id}/stop", response_model=JobResponse)
def stop_job(
    job_id: str,
    user: Annotated[User | None, Depends(optional_user)],
    guest_token: Annotated[str | None, Header(alias="X-Job-Token")] = None,
) -> JobResponse:
    require_job_access(require_job(job_id), user, guest_token)
    job = job_store.stop(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.status != "stopped":
        raise HTTPException(status_code=409, detail="任务已结束，无法停止")
    if job.results:
        write_csv(job)
        job_store.persist(job)
    return serialize_job(job)


@router.post("/jobs/{job_id}/resume", response_model=JobResponse, status_code=202)
def resume_job(
    job_id: str,
    user: Annotated[User | None, Depends(optional_user)],
    guest_token: Annotated[str | None, Header(alias="X-Job-Token")] = None,
) -> JobResponse:
    job = require_job_access(require_job(job_id), user, guest_token)
    if job.status != "stopped":
        raise HTTPException(status_code=409, detail="只有已停止的任务可以继续验证")
    try:
        continuation = submit_stopped_job_continuation(job)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return serialize_job(continuation)


@router.get("/jobs/{job_id}/results", response_model=ResultsResponse)
def get_results(
    job_id: str,
    user: Annotated[User | None, Depends(optional_user)],
    guest_token: Annotated[str | None, Header(alias="X-Job-Token")] = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    search: str = Query(default="", max_length=256),
    deliverability: str = Query(default="all", pattern="^(all|deliverable|undeliverable|unknown)$"),
) -> ResultsResponse:
    job = require_job_access(require_job(job_id), user, guest_token)
    query = search.strip().lower()
    filtered_results = [
        normalize_result(result)
        for result in job.results
        if (not query or query in str(result.get("email", "")).lower())
        and (
            deliverability == "all"
            or (deliverability == "deliverable" and result.get("deliverable") is True)
            or (deliverability == "undeliverable" and result.get("deliverable") is False)
            or (deliverability == "unknown" and result.get("deliverable") is None and not result.get("skipped"))
        )
    ]
    return ResultsResponse(
        total=len(job.emails),
        available=len(filtered_results),
        offset=offset,
        limit=limit,
        items=filtered_results[offset : offset + limit],
    )


@router.get("/jobs/{job_id}/download")
def download_results(
    job_id: str,
    user: Annotated[User | None, Depends(optional_user)],
    guest_token: Annotated[str | None, Header(alias="X-Job-Token")] = None,
) -> FileResponse:
    job = require_job_access(require_job(job_id), user, guest_token)
    if job.status not in {"completed", "stopped"} or job.csv_path is None or not job.csv_path.exists():
        raise HTTPException(status_code=409, detail="结果文件尚未生成")
    return FileResponse(
        job.csv_path,
        media_type="text/csv; charset=utf-8",
        filename=verification_filename(job),
    )


@router.post("/import", response_model=ImportResponse)
async def import_file(file: Annotated[UploadFile, File()]) -> ImportResponse:
    data = await file.read(settings.max_import_bytes + 1)
    if len(data) > settings.max_import_bytes:
        raise HTTPException(status_code=413, detail="文件不能超过 5 MB")
    try:
        emails = extract_emails(file.filename or "", data, settings.max_emails_per_job or None)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not emails:
        raise HTTPException(status_code=422, detail="文件中没有识别到邮箱地址")
    return ImportResponse(count=len(emails), emails=emails)
