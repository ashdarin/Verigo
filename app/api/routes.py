from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import json
import time
import uuid
import re
from datetime import datetime, time as datetime_time, timedelta, timezone
from zoneinfo import ZoneInfo
from io import StringIO
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response

from app.api.auth import (
    check_attempt_limit, optional_user, require_admin, require_user, request_network_hash,
)
from app.api.schemas import (
    AdminCreditAdjustmentResponse,
    AdminCreditGrantRequest,
    AdminRedemptionCodeCreateRequest,
    AdminRedemptionCodeCreateResponse,
    CreateJobRequest,
    DiscoveryRequest,
    DiscoveryResponse,
    DomainPreviewResponse,
    ImportResponse,
    JobResponse,
    NotificationListResponse,
    PaymentOrderRequest,
    PaymentOrderResponse,
    RedemptionCodeRequest,
    RedemptionCodeResponse,
    ProspectingRunRequest,
    ProspectingRunResponse,
    ProspectingRunPageResponse,
    ProspectingResultsResponse,
    ProspectingContactUpdateRequest,
    ProspectingCompanyDiscoverRequest,
    ProspectingCompanyImportResponse,
    ProspectingCompanyPageResponse,
    ProspectingCompanyUpdateRequest,
    SavedProspectingContactsResponse,
    ResultsResponse,
    ListCreateRequest, ListUpdateRequest, ListResultIdsRequest, SaveJobResultRequest, SaveJobResultsRequest, ReverifyRequest,
    SingleVerificationRequest,
    WorkerFailureRequest,
    WorkerResultsRequest,
)
from app.config import settings
from app.core.company_imports import extract_companies
from app.core.imports import extract_emails
from app.core.discovery import candidate_emails
from app.core.domain_relations import discover_related
from app.core.safe_http import has_only_public_addresses, safe_fetch
from app.core.prospecting import (
    generate_candidates,
    infer_email_pattern,
    normalize_company_domain,
    rerank_candidates,
)
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
from app.core.cloudshell_coordinator import cloudshell_coordinator
from app.core.provider_policy import (
    YAHOO_UNSUPPORTED_MESSAGE,
    is_qq_email,
    is_yahoo_email,
    is_yahoo_domain,
    yahoo_addresses,
)
from app.db.auth import User, auth_store
from app.db.domain_previews import domain_preview_store
from app.db.jobs import Job, job_store, utc_now
from app.db.metrics import metrics_store
from app.db.prospecting import ProspectingRun, prospecting_store
from app.db.result_objects import result_object_store
from app.tasks.verification import (
    clean_emails,
    job_progress,
    normalize_result,
    finalize_temporary_smtp_results,
    finish_background_retry,
    finish_background_retry_failure,
    finish_initial_job,
    apply_prospecting_receiver_protection,
    summarize,
    sync_parent_job,
    verification_filename,
    verification_tasks,
    write_csv,
    yahoo_unsupported_result,
)


router = APIRouter(prefix="/api")

def _normalize_domain_query(value: str) -> str:
    return value.strip().lower().replace("https://", "").replace("http://", "").removeprefix("www.").split("/", 1)[0]


def _domain_suggestions(query: str) -> list[dict[str, object]]:
    if "." in query:
        return []
    return domain_preview_store.suggestions(query)[:6]


def _has_only_public_addresses(domain: str) -> bool:
    """Require every resolved address to be publicly routable before fetching it."""
    return has_only_public_addresses(domain)
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
    "codearts": "codearts",
}
def remote_worker_label(execution_target: str) -> str:
    return {
        "tencent_qq": "腾讯 QQ 验证节点",
        DOMESTIC_CLOUDSTUDIO_TARGET: "国内邮箱 Cloud Studio 验证节点",
        GMAIL_TARGET: "Google Cloud Shell 验证节点",
        "codearts": "Huawei CodeArts 验证节点",
    }.get(execution_target, "远程验证节点")


def remote_worker_count(execution_target: str, requested_count: int) -> int:
    """Apply the target-specific concurrency cap before a remote job is queued."""
    if execution_target == "gmail":
        limit = settings.cloudshell_worker_max_workers
    elif execution_target == "codearts":
        limit = settings.codearts_worker_max_workers
    else:
        limit = settings.cloudstudio_worker_max_workers
    return max(1, min(requested_count, limit))


def require_job(job_id: str, *, include_results: bool = True) -> Job:
    job = job_store.get(job_id, include_results=include_results)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或服务已重启")
    return job


def require_verification_submission_open() -> None:
    if job_store.service_mode() == "draining":
        raise HTTPException(
            status_code=503,
            detail="Verification service is temporarily draining for maintenance. Please retry shortly.",
            headers={"Retry-After": "60"},
        )


def require_prospecting_beta(
    user: Annotated[User, Depends(require_user)],
) -> User:
    allowed = settings.prospecting_beta_allowed_emails
    if (
        not settings.prospecting_beta_enabled
        or not user.email_verified
        or not user.email
        or user.email.lower() not in allowed
    ):
        raise HTTPException(status_code=403, detail="该内测功能当前不可用")
    return user


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
    # Business domains such as company.com must not be excluded merely
    # because their TLD is longer than two characters.
    return bool(domain and "." in domain and not is_domestic_email_domain(domain))


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


def codearts_worker_allowed(owner_email: str | None) -> bool:
    allowed = settings.codearts_worker_allowed_emails
    return bool(
        settings.codearts_worker_enabled
        and ("*" in allowed or (owner_email and owner_email.lower() in allowed))
    )


def domestic_worker_allowed(owner_email: str | None) -> bool:
    allowed = settings.tencent_qq_worker_allowed_emails
    return bool(
        settings.cloudstudio_domestic_worker_enabled
        and ("*" in allowed or (owner_email and owner_email.lower() in allowed))
    )


def email_execution_target(email: str, owner_email: str | None, *, fast_local: bool = False) -> str:
    """Return the configured worker target for one address."""
    if is_qq_email(email):
        return "tencent_qq"
    domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    # Gmail and Microsoft consumer mailboxes have native local verification
    # paths. Keep single checks off the remote-node wake-up queue so a cold
    # CloudShell session cannot add avoidable latency.
    if fast_local and (
        domain in {"gmail.com", "googlemail.com"}
        or domain.startswith("outlook.")
        or domain.startswith("hotmail.")
        or domain.startswith("live.")
        or domain.startswith("msn.")
    ):
        return "local"
    if is_domestic_email_domain(domain) and domestic_worker_allowed(owner_email):
        return DOMESTIC_CLOUDSTUDIO_TARGET
    if is_domestic_email_domain(domain) and qq_worker_allowed(owner_email):
        return "tencent_qq"
    if is_foreign_email_domain(domain) and codearts_worker_allowed(owner_email):
        return "codearts"
    if is_foreign_email_domain(domain) and gmail_worker_allowed(owner_email):
        return "gmail"
    return "local"


def partition_target_emails(
    targets: dict[tuple[str, int], list[str]],
) -> list[tuple[str, list[str], int]]:
    """Keep remote completion requests below their maximum supported payload."""
    partitions: list[tuple[str, list[str], int]] = []
    remote_targets = {"tencent_qq", DOMESTIC_CLOUDSTUDIO_TARGET, "gmail", "codearts"}
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
    list_name: str | None = None,
) -> Job:
    targets: dict[tuple[str, int], list[str]] = {}
    for email in emails:
        target = (
            "unsupported"
            if is_yahoo_email(email)
            else email_execution_target(email, owner_email, fast_local=len(emails) == 1)
        )
        # QQ verification stays on Cloud Studio and is intentionally serial.
        # Cloud Studio otherwise retains its existing cap; Cloud Shell can use
        # eight processes when the user chooses Fastest mode.
        child_worker_count = (
            1
            if is_qq_email(email)
            else remote_worker_count(target, worker_count)
            if target in {"tencent_qq", DOMESTIC_CLOUDSTUDIO_TARGET, "gmail", "codearts"}
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
            list_name=list_name,
        )

    if stop_on_deliverable:
        # This mode must stop globally after the first deliverable result. It
        # cannot be leased to the sharded remote queue, even if all candidates
        # happen to route to one remote target; remote claims exclude it.
        return verification_tasks.submit(
            emails,
            worker_count,
            owner_id=owner_id,
            stop_on_deliverable=True,
            job_id=job_id,
            execution_target="local",
            list_name=list_name,
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
        list_name=list_name,
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
    enabled = {
        "tencent_qq": settings.tencent_qq_worker_enabled,
        DOMESTIC_CLOUDSTUDIO_TARGET: settings.cloudstudio_domestic_worker_enabled,
        "gmail": settings.gmail_worker_enabled,
        "codearts": settings.codearts_worker_enabled,
    }[execution_target]
    # A disabled target is a hard admission boundary. Existing remote processes
    # may still be alive after a maintenance pause, but they cannot claim,
    # heartbeat, or submit results until the operator explicitly re-enables it.
    if not enabled:
        raise HTTPException(status_code=503, detail="Remote verification node is disabled")
    configured_token = {
        "tencent_qq": settings.tencent_qq_worker_token,
        DOMESTIC_CLOUDSTUDIO_TARGET: settings.cloudstudio_domestic_worker_token,
        "gmail": settings.gmail_worker_token,
        "codearts": settings.codearts_worker_token,
    }[execution_target]
    if not configured_token:
        raise HTTPException(status_code=503, detail="远程验证节点尚未配置")
    if not token or not hmac.compare_digest(token, configured_token):
        raise HTTPException(status_code=401, detail="远程验证节点认证失败")
    return execution_target


def require_remote_job(job_id: str, worker_id: str, execution_target: str, lease_id: str | None = None) -> Job:
    job = require_job(job_id, include_results=False)
    valid_lease = bool(
        lease_id and job_store.lease_valid(job_id, worker_id, lease_id, execution_target)
    )
    if not valid_lease:
        raise HTTPException(status_code=409, detail="远程验证节点任务租约无效")
    if execution_target == "tencent_qq":
        worker_lifecycle.record_worker_seen(worker_id)
    elif execution_target == DOMESTIC_CLOUDSTUDIO_TARGET:
        domestic_worker_lifecycle.record_worker_seen(worker_id)
    elif execution_target == GMAIL_TARGET:
        cloudshell_lifecycle.record_worker_seen(worker_id)
    return job


def merge_worker_results(job: Job, results: list[dict[str, object]]) -> list[dict[str, object]]:
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
    return normalized


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
    overview = job_store.result_overview(job.id)
    total = len(job.emails)
    if overview.total or not job.results:
        completed = total if job.status == "completed" else min(overview.settled, total)
        progress = round((completed / total * 100) if total else 0, 1)
        summary = {
            "total": overview.total,
            "valid": overview.valid,
            "deliverable": overview.deliverable,
            "undeliverable": overview.undeliverable,
            "unknown": overview.unknown,
            "catch_all": overview.catch_all,
        }
    else:
        # Allows callers that are building a new, not-yet-persisted job to use
        # the same response serializer without depending on database state.
        completed, total, progress = job_progress(job)
        summary = summarize([normalize_result(result) for result in job.results])
    is_done = job.status in {"completed", "stopped"}
    retry_at = job.deferred_retry_at
    if overview.retry_at:
        retry_at = min([retry_at, overview.retry_at] if retry_at else [overview.retry_at])
    if job.execution_target == "aggregate":
        child_retry = job_store.earliest_child_retry(job.id)
        if child_retry:
            retry_at = min([retry_at, child_retry] if retry_at else [child_retry])
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
        summary=summary,
        download_url=f"/api/jobs/{job.id}/download" if is_done else None,
        download_name=verification_filename(job) if is_done else None,
        list_name=job.list_name,
        queue_position=job_store.queue_position(job.id),
        retry_at=retry_at.isoformat() if retry_at else None,
        stop_on_deliverable=job.stop_on_deliverable,
        qq_slow=any(is_qq_email(email) for email in job.emails),
        review_updated=overview.review_updated,
        access_token=job.guest_token,
    )


@router.get("/health")
def health() -> dict[str, object]:
    try:
        job_store.health_summary()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database is unavailable") from exc
    return {
        "status": "ok",
        "database": "ok",
    }


@router.get("/internal/readiness")
def readiness(
    token: Annotated[str | None, Header(alias="X-Verigo-Monitor-Token")] = None,
) -> dict[str, object]:
    configured_token = settings.monitor_token
    if not configured_token:
        raise HTTPException(status_code=503, detail="monitor token is not configured")
    if not token or not hmac.compare_digest(token, configured_token):
        raise HTTPException(status_code=401, detail="monitor authentication failed")
    try:
        summary = job_store.health_summary()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database is unavailable") from exc
    degraded = bool(summary["stale_leases"] or summary["unhealthy_targets"])
    return {
        "status": "degraded" if degraded else "ok",
        "database": "ok",
        **summary,
    }


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
    worker_capacity: Annotated[int, Header(alias="X-Verigo-Worker-Capacity")] = 1,
    wait_seconds: int = Query(default=20, ge=0, le=25),
) -> dict[str, object]:
    execution_target = require_remote_worker(worker_target, token)
    worker_name = (worker_id or "").strip()
    if not worker_name or len(worker_name) > 128:
        raise HTTPException(status_code=422, detail="腾讯 QQ 验证节点标识无效")
    if not 1 <= worker_capacity <= 128:
        raise HTTPException(status_code=422, detail="Remote worker capacity is invalid")
    job_store.record_worker_seen(execution_target, worker_name, worker_capacity)
    rotation_token: str | None = None
    if execution_target == GMAIL_TARGET:
        # Only the least-used account is allowed to claim the next Gmail shard.
        # A short reservation prevents concurrent polling processes from winning
        # the same rotation slot before the lease is committed.
        rotation_token = cloudshell_coordinator.reserve(
            worker_name,
            min(settings.scheduler_remote_shard_size, settings.remote_worker_max_emails_per_job),
        )
        if rotation_token is None:
            await asyncio.sleep(min(0.25, wait_seconds))
            return {"job": None}
    try:
        deadline = time.monotonic() + wait_seconds
        while True:
            job = job_store.claim_remote_lease(
                worker_name, execution_target, capacity=worker_capacity,
                shard_size=min(
                    settings.scheduler_remote_shard_size,
                    settings.remote_worker_max_emails_per_job,
                ),
                allow_local_fallback=True,
                prospecting_shard_size=settings.prospecting_scheduler_shard_size,
            )
            if job is not None:
                if execution_target == GMAIL_TARGET:
                    cloudshell_coordinator.commit(rotation_token or "", len(job.pending_indices))
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
                        "control_probe_email": prospecting_store.control_sample_for_job(job.id),
                    }
                }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"job": None}
            await asyncio.sleep(min(0.25, remaining))
    except Exception:
        if execution_target == GMAIL_TARGET:
            cloudshell_coordinator.release(rotation_token)
        raise
    finally:
        # A successful claim is committed above; release is a no-op afterwards.
        if execution_target == GMAIL_TARGET and rotation_token:
            cloudshell_coordinator.release(rotation_token)


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
    if job.status == "stopped":
        return {"status": "stopped", "stop_requested": True}
    worker_name = (worker_id or "").strip()
    job = require_remote_job(job_id, worker_name, execution_target, lease_id)
    if not lease_id or not job_store.heartbeat_lease(job.id, worker_name, lease_id):
        raise HTTPException(status_code=409, detail="Remote worker lease is no longer active")
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
    if job.status == "stopped":
        return {"status": "stopped", "stop_requested": True}
    worker_name = (worker_id or "").strip()
    job = require_remote_job(job_id, worker_name, execution_target, payload.lease_id)
    # A callback is acknowledged only after its result rows are durable. Lease
    # validation, the index check, row upsert, and renewal are one transaction.
    normalized = merge_worker_results(job, payload.results)
    if not payload.lease_id or not job_store.report_lease_results(
        job.id, worker_name, payload.lease_id, normalized, execution_target=execution_target,
    ):
        raise HTTPException(status_code=409, detail="Remote worker lease is no longer active")
    refreshed = job_store.get(job.id)
    if refreshed is None:
        raise HTTPException(status_code=404, detail="Verification job no longer exists")
    if job_store.reconcile_catch_all_conflicts(job.id):
        refreshed = job_store.get(job.id)
        if refreshed is None:
            raise HTTPException(status_code=404, detail="Verification job no longer exists")
    protected = apply_prospecting_receiver_protection(refreshed, payload.control_probes)
    if protected is not None:
        sync_parent_job(protected)
        return {
            "status": protected.status,
            "stop_requested": True,
            "accepted": len(payload.results),
            "persisted": len(payload.results),
        }
    if payload.results:
        sync_parent_job(job)
    return {
        "status": job.status,
        "stop_requested": False,
        "accepted": len(payload.results),
        "persisted": len(payload.results),
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
    if job.status == "stopped":
        return serialize_job(job)
    worker_name = (worker_id or "").strip()
    job = require_remote_job(job_id, worker_name, execution_target, payload.lease_id)
    normalized = merge_worker_results(job, payload.results)
    if not payload.lease_id or not job_store.complete_lease_with_results(
        job.id, worker_name, payload.lease_id, normalized, execution_target=execution_target,
    ):
        raise HTTPException(status_code=409, detail="Remote worker lease is no longer active")
    refreshed = job_store.get(job.id)
    if refreshed is None:
        raise HTTPException(status_code=404, detail="Verification job no longer exists")
    protected = apply_prospecting_receiver_protection(refreshed, payload.control_probes)
    if protected is not None:
        sync_parent_job(protected)
        return serialize_job(protected)
    if job_store.pending_count(job.id):
        sync_parent_job(job)
        return serialize_job(refreshed)
    job = refreshed
    prospecting_store.finalize_run(job.id, job.results)
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
    if job.status == "stopped":
        return serialize_job(job)
    worker_name = (worker_id or "").strip()
    job = require_remote_job(job_id, worker_name, execution_target, payload.lease_id)
    if not job_store.abandon_lease(job.id, worker_name, payload.lease_id):
        raise HTTPException(status_code=409, detail="Remote worker lease is no longer active")
    if execution_target == GMAIL_TARGET:
        cloudshell_coordinator.record_failure(worker_name, payload.error)
    job.error = f"{remote_worker_label(execution_target)} will retry: {payload.error}"
    job.status = "queued"
    job.worker_id = None
    job.heartbeat_at = None
    job_store.persist(job)
    sync_parent_job(job)
    return serialize_job(job_store.get(job.id) or job)


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


@router.get("/admin/cloudshell/accounts")
def admin_cloudshell_accounts(_: Annotated[User, Depends(require_admin)]) -> dict[str, object]:
    """Expose daily rotation counters without exposing ADC or SSH paths."""
    return cloudshell_coordinator.dashboard_snapshot()


@router.get("/notifications", response_model=NotificationListResponse)
def list_notifications(
    user: Annotated[User, Depends(require_user)],
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=30, ge=1, le=100),
) -> NotificationListResponse:
    items, unread_count, total = auth_store.list_notifications(user.id, offset=offset, limit=limit)
    return NotificationListResponse(
        items=items, unread_count=unread_count, total=total, offset=offset, limit=limit,
    )


@router.post("/notifications/read", status_code=204)
def mark_notifications_read(user: Annotated[User, Depends(require_user)]) -> None:
    auth_store.mark_notifications_read(user.id)


@router.post("/notifications/{notification_id}/read", status_code=204)
def mark_notification_read(
    notification_id: str, user: Annotated[User, Depends(require_user)],
) -> None:
    auth_store.mark_notification_read(user.id, notification_id)

@router.get("/wallet")
def wallet_snapshot(user: Annotated[User, Depends(require_user)]) -> dict[str, object]:
    return auth_store.wallet_snapshot(user.id)


@router.post("/wallet/redeem", response_model=RedemptionCodeResponse)
def redeem_credit_code(
    payload: RedemptionCodeRequest,
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> RedemptionCodeResponse:
    check_attempt_limit(
        f"redemption:{user.id}:{request_network_hash(request)}", limit=12, window=900
    )
    try:
        result = auth_store.redeem_credit_code(user.id, payload.code)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedemptionCodeResponse(**result)


@router.post(
    "/admin/redemption-codes",
    response_model=AdminRedemptionCodeCreateResponse,
    status_code=201,
)
def create_redemption_codes(
    payload: AdminRedemptionCodeCreateRequest,
    admin: Annotated[User, Depends(require_admin)],
) -> AdminRedemptionCodeCreateResponse:
    result = auth_store.create_redemption_codes(
        admin.id, payload.amount_yuan, payload.quantity
    )
    return AdminRedemptionCodeCreateResponse(**result)


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


@router.get("/domain-suggestions")
def domain_suggestions(q: str = Query(min_length=1, max_length=63)) -> dict[str, object]:
    """Fast prefix lookup; it never starts network discovery or legal parsing."""
    query = _normalize_domain_query(q)
    if "." in query or not re.fullmatch(r"[a-z0-9-]+", query):
        return {"query": query, "suggestions": []}
    return {"query": query, "suggestions": _domain_suggestions(query)}


@router.get("/domain-preview", response_model=DomainPreviewResponse)
def domain_preview(
    q: str = Query(min_length=1, max_length=253),
) -> DomainPreviewResponse:
    """Return a lightweight website identity preview for the finder domain."""
    domain = _normalize_domain_query(q)
    if "." not in domain:
        suggestions = _domain_suggestions(domain)
        return DomainPreviewResponse(
            domain=domain,
            url=suggestions[0]["url"] if suggestions else "",
            suggestions=suggestions,
        )
    if not re.fullmatch(r"(?=.{3,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", domain):
        raise HTTPException(status_code=422, detail="请输入有效的公司域名")
    cached = domain_preview_store.get(domain)
    if cached is not None:
        return DomainPreviewResponse(
            domain=domain,
            url=cached.get("url") or f"https://{domain}",
            title=cached.get("title"),
            reachable=bool(cached.get("reachable")),
            related_domains=cached.get("related_domains") or [],
            entities=cached.get("entities") or [],
            logo_url=cached.get("logo_url") or f"https://logos.hunter.io/{domain}",
            relations_pending=False,
        )
    if not _has_only_public_addresses(domain):
        return DomainPreviewResponse(domain=domain, url=f"https://{domain}")
    title = None
    reachable = False
    try:
        response = safe_fetch(f"https://{domain}", timeout=3, max_bytes=200_000,
                              allowed_hosts={domain, f"www.{domain}"})
        if response is not None:
            reachable = 200 <= response.status < 500
            sample = response.body.decode("utf-8", "ignore")
            match = re.search(r"<title[^>]*>(.*?)</title>", sample, re.I | re.S)
            if match:
                title = re.sub(r"\s+", " ", match.group(1)).strip()[:160] or None
    except Exception:
        pass
    return DomainPreviewResponse(domain=domain, url=f"https://{domain}", title=title, reachable=reachable,
        logo_url=f"https://logos.hunter.io/{domain}", relations_pending=True)

@router.get("/domain-relations", response_model=DomainPreviewResponse)
def domain_relations(q: str = Query(min_length=3, max_length=253)) -> DomainPreviewResponse:
    domain = _normalize_domain_query(q)
    if "." not in domain:
        suggestions = _domain_suggestions(domain)
        return DomainPreviewResponse(
            domain=domain,
            url=suggestions[0]["url"] if suggestions else "",
            suggestions=suggestions,
        )
    if not re.fullmatch(r"(?=.{3,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", domain):
        raise HTTPException(status_code=422, detail="请输入有效的公司域名")
    cached = domain_preview_store.get(domain)
    if cached is not None:
        return DomainPreviewResponse(
            domain=domain,
            url=cached.get("url") or f"https://{domain}",
            title=cached.get("title"),
            reachable=bool(cached.get("reachable")),
            related_domains=cached.get("related_domains") or [],
            entities=cached.get("entities") or [],
            logo_url=cached.get("logo_url") or f"https://logos.hunter.io/{domain}",
            relations_pending=False,
        )
    if not _has_only_public_addresses(domain):
        raise HTTPException(status_code=422, detail="Company domain must resolve only to public addresses")
    related_domains, entities = discover_related(domain)
    payload = {
        "domain": domain,
        "url": f"https://{domain}",
        "title": entities[0] if entities else None,
        "related_domains": related_domains,
        "entities": entities,
        "logo_url": f"https://logos.hunter.io/{domain}",
        "reachable": bool(related_domains),
    }
    domain_preview_store.put(domain, payload)
    return DomainPreviewResponse(domain=domain, url=payload["url"], title=payload["title"], related_domains=related_domains, entities=entities,
        logo_url=payload["logo_url"], relations_pending=False)


@router.post("/discovery/verify", response_model=JobResponse, status_code=202)
def verify_discovery_candidates(
    payload: DiscoveryRequest,
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> JobResponse:
    require_verification_submission_open()
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


def serialize_prospecting_run(run: ProspectingRun) -> ProspectingRunResponse:
    job = require_job(run.verification_job_id, include_results=False)
    overview = job_store.result_overview(job.id)
    total = len(job.emails)
    completed = total if job.status == "completed" else min(overview.settled, total)
    progress = round((completed / total * 100) if total else 0, 1)
    summary = {
        "total": overview.total,
        "valid": overview.valid,
        "deliverable": overview.deliverable,
        "undeliverable": overview.undeliverable,
        "unknown": overview.unknown,
        "verified": overview.deliverable - overview.catch_all,
        "catch_all": overview.catch_all,
    }
    return ProspectingRunResponse(
        id=run.id,
        domain=run.domain,
        country=run.country,
        requested_pattern=run.requested_pattern,
        verification_job_id=run.verification_job_id,
        status=job.status,
        created_at=run.created_at.isoformat(),
        total=total,
        completed=completed,
        progress=progress,
        error=job.error,
        profile_patterns=list(run.profile_patterns),
        summary=summary,
        result_total=prospecting_store.result_count(run.id),
        saved_count=prospecting_store.saved_contact_count(run.owner_id),
        protection=prospecting_store.protection_status(run.domain),
    )


def prospecting_results_page(
    run: ProspectingRun, user: User, *, offset: int, limit: int,
) -> tuple[int, list[dict[str, Any]]]:
    """Place demand-gated shared confirmations ahead of this run's new work."""
    current_candidate_emails = {item["email"] for item in prospecting_store.candidates(run.id)}
    shared_total, shared = prospecting_store.confirmed_contacts_for_requested_domain(
        run.domain, exclude_emails=current_candidate_emails, offset=offset, limit=limit,
    )
    local_offset = max(0, offset - shared_total)
    local_limit = max(0, limit - len(shared))
    local_total, local = prospecting_store.result_page(
        run, offset=local_offset, limit=local_limit,
    ) if local_limit else (prospecting_store.result_count(run.id), [])
    return shared_total + local_total, [*shared, *local]


def submit_prospecting_run(
    payload: ProspectingRunRequest,
    user: User,
    *,
    business_entry_only: bool = False,
) -> ProspectingRunResponse:
    require_verification_submission_open()
    try:
        domain = normalize_company_domain(payload.domain)
        blocked_until = prospecting_store.blocked_until(domain)
        if blocked_until is not None:
            raise HTTPException(
                status_code=429,
                detail=f"Discovery for this domain is temporarily protected until {blocked_until.isoformat()}",
            )
        known_pattern = None
        known_email = None
        if payload.known_email:
            known_pattern = infer_email_pattern(
                domain, payload.known_first_name or "", payload.known_last_name or "", payload.known_email
            )
            known_email = payload.known_email.strip().lower()
            prospecting_store.record_provided_pattern(user.id, domain, known_pattern)
        learned_patterns = prospecting_store.domain_patterns(user.id, domain)
        issued_emails, issued_name_keys = prospecting_store.issued_candidate_keys(domain)
        # Keep enough catalogue entries beyond prior runs that filtering cannot
        # prematurely exhaust the selected or verified naming convention.
        catalogue_budget = max(
            settings.prospecting_beta_catalogue_candidates,
            len(issued_emails) + len(issued_name_keys) + settings.prospecting_beta_max_candidates + 1,
        )
        catalogue = generate_candidates(
            domain,
            payload.country,
            catalogue_budget,
            learned_patterns,
            known_pattern or payload.email_pattern,
        )
        candidates = rerank_candidates(
            candidate for candidate in catalogue
            if candidate.email not in issued_emails
            and candidate.email != known_email
            and (candidate.name_key is None or candidate.name_key not in issued_name_keys)
        )[:settings.prospecting_beta_max_candidates]
        if business_entry_only:
            candidates = candidates[:1]
        if not candidates:
            if known_pattern or payload.email_pattern or learned_patterns:
                raise ValueError(
                    "The selected or verified email naming rule has no new name combinations. "
                    "Import a larger country name catalogue or explicitly choose another rule."
                )
            raise ValueError("All available unique candidates for this account and domain have already been checked")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    claim_token, candidates = prospecting_store.allocate_candidates(
        domain,
        candidates,
        1 if business_entry_only else settings.prospecting_beta_max_candidates,
    )
    if not candidates:
        raise HTTPException(status_code=422, detail="All available unique candidates for this account and domain have already been checked")
    job: Job | None = None
    try:
        # Keep company discovery in the shared local pool. Remote nodes may
        # steal its small leases when idle, but no single target owns the run.
        job = verification_tasks.submit(
            [candidate.email for candidate in candidates],
            worker_count=settings.max_workers_per_job,
            owner_id=user.id,
            stop_on_deliverable=False,
            job_id=uuid.uuid4().hex[:12],
            execution_target="local",
        )
        run = prospecting_store.create_run(
            user.id, domain, payload.country, known_pattern or payload.email_pattern, job.id, candidates, learned_patterns,
            claim_token=claim_token,
        )
        # The run is now registered, so all worker pools can apply the
        # discovery-specific MX ceiling before they start claiming shards.
        worker_lifecycle.notify_job_queued()
        domestic_worker_lifecycle.notify_job_queued()
        notify_cloudshell_job_queued()
    except RuntimeError as exc:
        prospecting_store.release_candidate_claim(claim_token)
        if job is not None:
            job_store.stop(job.id)
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except Exception:
        prospecting_store.release_candidate_claim(claim_token)
        if job is not None:
            job_store.stop(job.id)
        raise
    return serialize_prospecting_run(run)


@router.post("/prospecting-beta/runs", response_model=ProspectingRunResponse, status_code=202)
def create_prospecting_run(
    payload: ProspectingRunRequest,
    user: Annotated[User, Depends(require_prospecting_beta)],
) -> ProspectingRunResponse:
    return submit_prospecting_run(payload, user)


@router.post("/prospecting-beta/companies/import", response_model=ProspectingCompanyImportResponse)
async def import_prospecting_companies(
    file: Annotated[UploadFile, File()],
    user: Annotated[User, Depends(require_prospecting_beta)],
) -> ProspectingCompanyImportResponse:
    data = await file.read(settings.max_import_bytes + 1)
    if len(data) > settings.max_import_bytes:
        raise HTTPException(status_code=413, detail="Import file is too large")
    try:
        companies = extract_companies(
            file.filename or "companies.csv", data, settings.prospecting_company_import_max_rows
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not companies:
        raise HTTPException(status_code=422, detail="No companies were found in this file")
    import_id, imported = prospecting_store.import_companies(user.id, companies)
    return ProspectingCompanyImportResponse(import_id=import_id, imported=imported)


@router.get("/prospecting-beta/companies", response_model=ProspectingCompanyPageResponse)
def list_prospecting_companies(
    user: Annotated[User, Depends(require_prospecting_beta)],
    import_id: str | None = Query(default=None, min_length=8, max_length=32),
    search: str = Query(default="", max_length=128),
    domain_state: str = Query(default="all", pattern="^(all|ready|missing)$"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> ProspectingCompanyPageResponse:
    total, items = prospecting_store.company_page(
        user.id, import_id=import_id, search=search.strip(), domain_state=domain_state,
        offset=offset, limit=limit,
    )
    return ProspectingCompanyPageResponse(total=total, offset=offset, limit=limit, items=items)


@router.patch("/prospecting-beta/companies/{company_id}")
def update_prospecting_company(
    company_id: str,
    payload: ProspectingCompanyUpdateRequest,
    user: Annotated[User, Depends(require_prospecting_beta)],
) -> dict[str, Any]:
    try:
        domain = normalize_company_domain(payload.domain) if payload.domain and payload.domain.strip() else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    company = prospecting_store.update_company(
        user.id, company_id, domain=domain, country=payload.country, selected=payload.selected,
    )
    if company is None:
        raise HTTPException(status_code=404, detail="Company does not exist")
    return company


@router.post("/prospecting-beta/companies/discover", status_code=202)
def discover_selected_prospecting_companies(
    payload: ProspectingCompanyDiscoverRequest,
    user: Annotated[User, Depends(require_prospecting_beta)],
) -> dict[str, Any]:
    require_verification_submission_open()
    company_ids = list(dict.fromkeys(payload.company_ids))
    companies = prospecting_store.selected_companies(user.id, company_ids)
    by_id = {company["id"]: company for company in companies}
    runs: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    for company_id in company_ids:
        company = by_id.get(company_id)
        if company is None:
            skipped.append({"id": company_id, "reason": "Company does not exist"})
            continue
        if not company["selected"]:
            skipped.append({"id": company_id, "reason": "Company is not selected"})
            continue
        if not company["domain"]:
            skipped.append({"id": company_id, "reason": "A company domain is required"})
            continue
        country = company["country"] if company["country"] in {"US", "GB", "DE", "FR", "IT", "ES", "CN", "JP", "KR", "IN", "CA", "AU", "NL", "SE", "CH", "BR", "MX", "PL", "TR", "OTHER"} else payload.country
        try:
            run = submit_prospecting_run(
                ProspectingRunRequest(domain=company["domain"], country=country), user,
                business_entry_only=True,
            )
        except HTTPException as exc:
            skipped.append({"id": company_id, "reason": str(exc.detail)})
            continue
        prospecting_store.attach_company_run(user.id, company_id, run.id)
        runs.append({"company_id": company_id, "run_id": run.id, "domain": run.domain})
    return {"runs": runs, "skipped": skipped}


@router.get("/prospecting-beta/runs/{run_id}/results", response_model=ProspectingResultsResponse)
def list_prospecting_run_results(
    run_id: str,
    user: Annotated[User, Depends(require_prospecting_beta)],
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> ProspectingResultsResponse:
    run = prospecting_store.get(run_id, user.id)
    if run is None:
        raise HTTPException(status_code=404, detail="Prospecting run does not exist")
    # This route is only reachable after a user has requested this company
    # through the verified private-beta workflow; it is not a browseable pool.
    total, items = prospecting_results_page(run, user, offset=offset, limit=limit)
    return ProspectingResultsResponse(total=total, offset=offset, limit=limit, items=items)


@router.get("/prospecting-beta/runs/{run_id}/candidates")
def list_prospecting_run_candidates(
    run_id: str,
    user: Annotated[User, Depends(require_prospecting_beta)],
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, Any]:
    """Private-beta visibility into the exact generated names for review."""
    run = prospecting_store.get(run_id, user.id)
    if run is None:
        raise HTTPException(status_code=404, detail="Prospecting run does not exist")
    total, items = prospecting_store.candidate_page(run.id, offset=offset, limit=limit)
    return {"total": total, "offset": offset, "limit": limit, "items": items}


@router.get("/prospecting-beta/saved-contacts", response_model=SavedProspectingContactsResponse)
def list_saved_prospecting_contacts(
    user: Annotated[User, Depends(require_prospecting_beta)],
    domain: str | None = Query(default=None, min_length=3, max_length=253),
    search: str = Query(default="", max_length=128),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    domain_offset: int = Query(default=0, ge=0),
    domain_limit: int = Query(default=50, ge=1, le=100),
) -> SavedProspectingContactsResponse:
    try:
        normalized_domain = normalize_company_domain(domain) if domain else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    total, items = prospecting_store.saved_contacts(
        user.id, domain=normalized_domain, search=search.strip(), offset=offset, limit=limit,
    )
    domain_total, domains = prospecting_store.saved_contact_domains(
        user.id, search=search.strip(), offset=domain_offset, limit=domain_limit,
    )
    return SavedProspectingContactsResponse(
        workspace_total=prospecting_store.saved_contact_count(user.id),
        total=total,
        items=items,
        domains=domains,
        offset=offset,
        limit=limit,
        domain_total=domain_total,
        domain_offset=domain_offset,
        domain_limit=domain_limit,
    )


@router.patch("/prospecting-beta/saved-contacts")
def update_saved_prospecting_contact(
    payload: ProspectingContactUpdateRequest,
    user: Annotated[User, Depends(require_prospecting_beta)],
) -> dict[str, Any]:
    contact = prospecting_store.update_saved_contact(
        user.id, payload.email, favorite=payload.favorite, tags=payload.tags,
    )
    if contact is None:
        raise HTTPException(status_code=404, detail="Saved contact does not exist")
    return contact


@router.get("/prospecting-beta/companies/{domain}")
def get_prospecting_company(
    domain: str,
    user: Annotated[User, Depends(require_prospecting_beta)],
) -> dict[str, Any]:
    try:
        normalized_domain = normalize_company_domain(domain)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    snapshot = prospecting_store.company_snapshot(user.id, normalized_domain)
    if not snapshot["contact_count"]:
        raise HTTPException(status_code=404, detail="No saved contacts for this domain")
    return snapshot


@router.get("/prospecting-beta/saved-contacts/export")
def export_saved_prospecting_contacts(
    user: Annotated[User, Depends(require_prospecting_beta)],
    domain: str | None = Query(default=None, min_length=3, max_length=253),
    search: str = Query(default="", max_length=128),
) -> Response:
    try:
        normalized_domain = normalize_company_domain(domain) if domain else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    total, contacts = prospecting_store.saved_contacts(
        user.id, domain=normalized_domain, search=search.strip(), offset=0, limit=100000,
    )
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=(
        "email", "domain", "category", "pattern", "source", "last_verified_at",
        "verification_method", "confidence", "favorite", "tags",
    ))
    writer.writeheader()
    for contact in contacts:
        writer.writerow({
            "email": contact["email"], "domain": contact["domain"],
            "category": contact["category"], "pattern": contact["pattern"],
            "source": contact["source"], "last_verified_at": contact["last_verified_at"],
            "verification_method": contact["verification_method"],
            "confidence": contact["confidence"], "favorite": contact["favorite"],
            "tags": ",".join(contact["tags"]),
        })
    filename = f"verigo-contacts-{normalized_domain or 'all'}-{total}.csv"
    return Response(
        output.getvalue().encode("utf-8-sig"), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
@router.get("/prospecting-beta/runs", response_model=ProspectingRunPageResponse)
def list_prospecting_runs(
    user: Annotated[User, Depends(require_prospecting_beta)],
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=50),
) -> ProspectingRunPageResponse:
    total, runs = prospecting_store.page_for_owner(user.id, offset=offset, limit=limit)
    return ProspectingRunPageResponse(
        total=total, offset=offset, limit=limit,
        items=[serialize_prospecting_run(run) for run in runs],
    )


@router.get("/prospecting-beta/runs/{run_id}", response_model=ProspectingRunResponse)
def get_prospecting_run(
    run_id: str,
    user: Annotated[User, Depends(require_prospecting_beta)],
) -> ProspectingRunResponse:
    run = prospecting_store.get(run_id, user.id)
    if run is None:
        raise HTTPException(status_code=404, detail="内测任务不存在")
    return serialize_prospecting_run(run)


@router.post("/prospecting-beta/runs/{run_id}/stop", response_model=ProspectingRunResponse)
def stop_prospecting_run(
    run_id: str,
    user: Annotated[User, Depends(require_prospecting_beta)],
) -> ProspectingRunResponse:
    run = prospecting_store.get(run_id, user.id)
    if run is None:
        raise HTTPException(status_code=404, detail="内测任务不存在")
    stopped_job = job_store.stop(run.verification_job_id)
    if stopped_job is None:
        raise HTTPException(status_code=404, detail="Verification job no longer exists")
    return serialize_prospecting_run(run)


@router.post("/jobs", response_model=JobResponse, status_code=202)
def create_job(
    payload: CreateJobRequest,
    request: Request,
    user: Annotated[User, Depends(require_user)],
) -> JobResponse:
    require_verification_submission_open()
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
            list_name=payload.list_name,
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
    require_verification_submission_open()
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
            list_name=payload.list_name,
        )
        if user and user.onboarding_required and user.email_verified and not user.activation_completed_at:
            auth_store.record_activation_job(user.id, job.id)
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
    if not settings.payment_checkout_url or not settings.payment_webhook_token:
        raise HTTPException(status_code=503, detail="支付通道暂未配置，请稍后再试")
    order = auth_store.create_payment_order(user.id, payload.packages)
    checkout_url = None
    if settings.payment_checkout_url:
        checkout_url = settings.payment_checkout_url.format(
            order_id=quote(str(order["id"]), safe=""),
            amount_fen=quote(str(order["amount_fen"]), safe=""),
            credits=quote(str(order["credits"]), safe=""),
            return_url=quote("https://verigo.site/wallet", safe=""),
        )
    return PaymentOrderResponse(
        **order, checkout_url=checkout_url, payment_enabled=bool(checkout_url)
    )


@router.post("/billing/webhook", include_in_schema=False)
async def payment_webhook(
    request: Request,
    signature: Annotated[str | None, Header(alias="X-Verigo-Payment-Signature")] = None,
) -> dict[str, object]:
    """Payment-gateway callback. The gateway signs the exact JSON request body."""
    if not settings.payment_webhook_token:
        raise HTTPException(status_code=404, detail="支付回调未配置")
    body = await request.body()
    expected = hmac.new(
        settings.payment_webhook_token.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=401, detail="支付回调签名无效")
    try:
        payload = json.loads(body)
        order_id = str(payload["order_id"])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="支付回调缺少订单号") from exc
    try:
        return auth_store.complete_payment_order(order_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/jobs")
def list_jobs(
    user: Annotated[User, Depends(require_user)],
    offset: int | None = Query(default=None, ge=0),
    limit: int = Query(default=20, ge=1, le=50),
    search: str = Query(default="", max_length=200),
    status: str = Query(default="all", max_length=20),
) -> dict[str, object] | list[JobResponse]:
    if status not in {"all", "queued", "running", "completed", "failed", "stopped"}:
        raise HTTPException(status_code=422, detail="Invalid history status")
    # Keep an old cached browser functional while the new page explicitly
    # opts into the paged response by supplying offset.
    if offset is None:
        return [serialize_job(job) for job in job_store.list_recent(user.id, limit)]
    total, jobs = job_store.page_for_owner(
        user.id, offset=offset, limit=limit, search=search, status=status
    )
    return {
        "total": total, "offset": offset, "limit": limit,
        "items": [serialize_job(job) for job in jobs],
    }

@router.get("/workspace")
def workspace_snapshot(user: Annotated[User, Depends(require_user)]) -> dict[str, object]:
    """Return workspace aggregates with server-side Asia/Shanghai day semantics."""
    shanghai = ZoneInfo("Asia/Shanghai")
    today = datetime.now(shanghai).date()
    day_start = datetime.combine(today, datetime_time.min, tzinfo=shanghai).astimezone(timezone.utc)
    day_end = datetime.combine(today + timedelta(days=1), datetime_time.min, tzinfo=shanghai).astimezone(timezone.utc)
    overview = job_store.workspace_overview(
        user.id, day_start=day_start, day_end=day_end,
    )
    jobs = job_store.list_recent(user.id, 8)
    return {
        "total": overview.total,
        "processed_today": overview.processed_today,
        "deliverable": overview.deliverable,
        "settled": overview.settled,
        "items": [serialize_job(job) for job in jobs],
        "recent_results": result_object_store.recent_results(user.id, 8),
    }


@router.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(
    job_id: str,
    user: Annotated[User | None, Depends(optional_user)],
    guest_token: Annotated[str | None, Header(alias="X-Job-Token")] = None,
) -> JobResponse:
    return serialize_job(require_job_access(require_job(job_id, include_results=False), user, guest_token))


@router.post("/jobs/{job_id}/reviewed", status_code=204)
def mark_job_reviewed(
    job_id: str,
    user: Annotated[User | None, Depends(optional_user)],
    guest_token: Annotated[str | None, Header(alias="X-Job-Token")] = None,
) -> None:
    job = require_job_access(require_job(job_id, include_results=False), user, guest_token)
    job_store.clear_job_review_updates(job.id)


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
    require_verification_submission_open()
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
    job = require_job_access(require_job(job_id, include_results=False), user, guest_token)
    available, page = job_store.result_page(
        job.id,
        offset=offset,
        limit=limit,
        search=search.strip(),
        deliverability=deliverability,
    )
    return ResultsResponse(
        total=len(job.emails),
        available=available,
        offset=offset,
        limit=limit,
        items=[normalize_result(result) for result in page],
    )


def _ensure_saved_result(user: User, job_id: str, result_index: int, guest_token: str | None = None) -> dict[str, object]:
    job = require_job_access(require_job(job_id, include_results=False), user, guest_token)
    if result_index < 0 or result_index >= len(job.emails):
        raise HTTPException(status_code=404, detail="Verification result does not exist")
    page = job_store.result_page(job.id, offset=result_index, limit=1, search="", deliverability="all")[1]
    raw = page[0] if page else {"email": job.emails[result_index], "progress_state": "pending", "original_index": result_index}
    normalized = normalize_result(raw)
    source = "discovery" if job.execution_target == "discovery" else ("reverify" if job.execution_target == "reverify" else ("single" if len(job.emails) == 1 else "batch"))
    return result_object_store.ensure_result(user.id, job.id, result_index, normalized, source)


@router.post("/results/save")
def save_job_result(payload: SaveJobResultRequest, user: Annotated[User, Depends(require_user)], guest_token: Annotated[str | None, Header(alias="X-Job-Token")] = None) -> dict[str, object]:
    result = _ensure_saved_result(user, payload.job_id, payload.result_index, guest_token)
    list_id = payload.list_id
    if not list_id:
        if not payload.list_name:
            raise HTTPException(status_code=422, detail="list_id or list_name is required")
        try:
            list_id = result_object_store.create_list(user.id, payload.list_name)["id"]
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    source = "discovery" if result.get("source") == "discovery" else ("batch" if result.get("source") == "batch" else "single")
    saved = result_object_store.add_results(user.id, list_id, [result["id"]], source)
    result = result_object_store.get_result(user.id, result["id"]) or result
    return {"result": result, **saved}

@router.post("/results/save-batch")
def save_job_results(payload: SaveJobResultsRequest, user: Annotated[User, Depends(require_user)], guest_token: Annotated[str | None, Header(alias="X-Job-Token")] = None) -> dict[str, object]:
    if not payload.list_id and not payload.list_name:
        raise HTTPException(status_code=422, detail="list_id or list_name is required")
    if payload.list_id and not result_object_store.get_list(user.id, payload.list_id):
        raise HTTPException(status_code=404, detail="list not found")
    job = require_job_access(require_job(payload.job_id, include_results=False), user, guest_token)
    invalid = [index for index in dict.fromkeys(payload.result_indices) if index < 0 or index >= len(job.emails)]
    if invalid:
        raise HTTPException(status_code=404, detail="Verification result does not exist")
    if payload.list_id:
        list_id = payload.list_id
    else:
        try:
            list_id = result_object_store.create_list(user.id, payload.list_name or "")["id"]
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    saved_results = [_ensure_saved_result(user, payload.job_id, index, guest_token) for index in dict.fromkeys(payload.result_indices)]
    source = "discovery" if any(item.get("source") == "discovery" for item in saved_results) else "batch"
    saved = result_object_store.add_results(user.id, list_id, [item["id"] for item in saved_results], source)
    return {"results": [result_object_store.get_result(user.id, item["id"]) or item for item in saved_results], **saved}


@router.get("/lists")
def get_lists(user: Annotated[User, Depends(require_user)]) -> list[dict[str, object]]:
    return result_object_store.list_lists(user.id)


@router.post("/lists")
def create_list(payload: ListCreateRequest, user: Annotated[User, Depends(require_user)]) -> dict[str, object]:
    try: return result_object_store.create_list(user.id, payload.name, payload.description)
    except ValueError as exc: raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/lists/{list_id}")
def get_list(list_id: str, user: Annotated[User, Depends(require_user)], offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=500), status: str = Query("all")) -> dict[str, object]:
    if status not in {"all", "deliverable", "undeliverable", "unknown", "catch-all", "queued", "running", "completed", "failed"}:
        raise HTTPException(status_code=422, detail="invalid list status")
    try:
        total, items = result_object_store.list_results(user.id, list_id, offset, limit, status)
        summary = result_object_store.get_list(user.id, list_id)
        return {"list": summary, "total": total, "offset": offset, "limit": limit, "items": items}
    except ValueError as exc: raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/lists/{list_id}")
def update_list(list_id: str, payload: ListUpdateRequest, user: Annotated[User, Depends(require_user)]) -> dict[str, object]:
    try:
        return result_object_store.update_list(
            user.id,
            list_id,
            name=payload.name,
            description=payload.description,
        )
    except ValueError as exc:
        status_code = 404 if str(exc) == "list not found" else 422
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.delete("/lists/{list_id}", status_code=204)
def archive_list(list_id: str, user: Annotated[User, Depends(require_user)]) -> None:
    try:
        result_object_store.archive_list(user.id, list_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/lists/{list_id}/results")
def add_list_results(list_id: str, payload: ListResultIdsRequest, user: Annotated[User, Depends(require_user)]) -> dict[str, object]:
    try: return result_object_store.add_results(user.id, list_id, payload.result_ids, payload.added_from)
    except ValueError as exc: raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/lists/{list_id}/results")
def remove_list_results(list_id: str, payload: ListResultIdsRequest, user: Annotated[User, Depends(require_user)]) -> dict[str, object]:
    try: return result_object_store.remove_results(user.id, list_id, payload.result_ids)
    except ValueError as exc: raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/lists/{list_id}/export")
def export_list(list_id: str, user: Annotated[User, Depends(require_user)]) -> Response:
    try: rows = result_object_store.export_rows(user.id, list_id)
    except ValueError as exc: raise HTTPException(status_code=404, detail=str(exc)) from exc
    output = StringIO(); writer = csv.writer(output); writer.writerow(["email", "status", "verification_method", "server_response", "confidence", "source", "task_id", "created_at"])
    for row in rows: writer.writerow([row.get(key) or "" for key in ("email", "status", "verification_method", "server_response", "confidence", "source", "task_id", "created_at")])
    return Response(content=output.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="verigo-list-{list_id}.csv"'})


@router.get("/results/{result_id}")
def get_saved_result(result_id: str, user: Annotated[User, Depends(require_user)]) -> dict[str, object]:
    result = result_object_store.get_result(user.id, result_id)
    if not result: raise HTTPException(status_code=404, detail="result not found")
    return result


@router.get("/results/{result_id}/history")
def get_saved_result_history(result_id: str, user: Annotated[User, Depends(require_user)]) -> dict[str, object]:
    try:
        return result_object_store.result_history(user.id, result_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

@router.post("/results/{result_id}/reverify", response_model=JobResponse, status_code=202)
def reverify_saved_result(result_id: str, user: Annotated[User, Depends(require_user)]) -> JobResponse:
    require_verification_submission_open()
    result = result_object_store.get_result(user.id, result_id)
    if not result:
        raise HTTPException(status_code=404, detail="result not found")
    try:
        job_id = uuid.uuid4().hex[:12]
        charged = 0 if is_yahoo_email(result["email"]) else 1
        if charged:
            auth_store.consume_credits(user.id, charged, f"reverify:{job_id}")
        job = submit_routed_job([result["email"]], 1, owner_id=user.id, owner_email=user.email, job_id=job_id)
        result_object_store.ensure_result(
            user.id,
            job.id,
            0,
            {"email": result["email"], "progress_state": "pending", "supersedes_result_id": result_id},
            "reverify",
        )
    except (ValueError, RuntimeError) as exc:
        if charged:
            auth_store.refund_credits(user.id, charged, f"reverify:{job_id}")
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return serialize_job(job)

@router.post("/lists/{list_id}/reverify", response_model=JobResponse, status_code=202)
def reverify_list_results(list_id: str, payload: ReverifyRequest, user: Annotated[User, Depends(require_user)]) -> JobResponse:
    require_verification_submission_open()
    try:
        _, rows = result_object_store.list_results(user.id, list_id, 0, 5000)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    selected = set(payload.result_ids)
    selected_rows = [row for row in rows if row["id"] in selected]
    emails = [row["email"] for row in selected_rows]
    if not emails:
        raise HTTPException(status_code=422, detail="列表中没有可再次验证的结果")
    job_id = uuid.uuid4().hex[:12]
    charged = sum(1 for email in emails if not is_yahoo_email(email))
    try:
        if charged:
            auth_store.consume_credits(user.id, charged, f"reverify:{job_id}")
        job = submit_routed_job(emails, 2, owner_id=user.id, owner_email=user.email, job_id=job_id)
        for index, previous in enumerate(selected_rows):
            result_object_store.ensure_result(
                user.id,
                job.id,
                index,
                {"email": previous["email"], "progress_state": "pending", "supersedes_result_id": previous["id"]},
                "reverify",
            )
    except (ValueError, RuntimeError) as exc:
        if charged:
            auth_store.refund_credits(user.id, charged, f"reverify:{job_id}")
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return serialize_job(job)


@router.post("/jobs/{job_id}/results/{result_index}/reviewed", status_code=204)
def mark_result_reviewed(
    job_id: str,
    result_index: int,
    user: Annotated[User | None, Depends(optional_user)],
    guest_token: Annotated[str | None, Header(alias="X-Job-Token")] = None,
) -> None:
    job = require_job_access(require_job(job_id, include_results=False), user, guest_token)
    if result_index < 0 or result_index >= len(job.emails):
        raise HTTPException(status_code=404, detail="Verification result does not exist")
    job_store.clear_result_review_update(job.id, result_index)
    if user is not None:
        auth_store.mark_result_notifications_read(user.id, job.id, result_index)


@router.get("/jobs/{job_id}/download")
def download_results(
    job_id: str,
    user: Annotated[User | None, Depends(optional_user)],
    guest_token: Annotated[str | None, Header(alias="X-Job-Token")] = None,
) -> FileResponse:
    job = require_job_access(require_job(job_id, include_results=False), user, guest_token)
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
