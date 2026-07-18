from __future__ import annotations

import asyncio
import hmac
import json
import time
import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse

from app.api.auth import optional_user, require_admin, require_user, request_network_hash
from app.api.schemas import (
    CreateJobRequest,
    DiscoveryRequest,
    DiscoveryResponse,
    ImportResponse,
    JobResponse,
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
from app.db.auth import User, auth_store
from app.db.jobs import Job, job_store, utc_now
from app.db.metrics import metrics_store
from app.tasks.verification import (
    clean_emails,
    job_progress,
    normalize_result,
    summarize,
    verification_filename,
    verification_tasks,
    write_csv,
)


router = APIRouter(prefix="/api")
TENCENT_QQ_DOMAINS = frozenset({"qq.com", "vip.qq.com", "foxmail.com"})


def require_job(job_id: str) -> Job:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或服务已重启")
    return job


def tencent_qq_target(emails: list[str]) -> str:
    if not settings.tencent_qq_worker_enabled or not emails:
        return "local"
    domains = {email.rsplit("@", 1)[-1].lower() for email in emails if "@" in email}
    return "tencent_qq" if domains and domains <= TENCENT_QQ_DOMAINS else "local"


def require_tencent_worker(token: str | None) -> None:
    configured_token = settings.tencent_qq_worker_token
    if not configured_token:
        raise HTTPException(status_code=503, detail="腾讯 QQ 验证节点尚未配置")
    if not token or not hmac.compare_digest(token, configured_token):
        raise HTTPException(status_code=401, detail="腾讯 QQ 验证节点认证失败")


def require_tencent_job(job_id: str, worker_id: str) -> Job:
    job = require_job(job_id)
    if job.execution_target != "tencent_qq" or job.worker_id != worker_id:
        raise HTTPException(status_code=409, detail="腾讯 QQ 验证节点任务租约无效")
    return job


def merge_worker_results(job: Job, results: list[dict[str, object]]) -> Job:
    by_index = {
        int(item.get("original_index", index)): dict(item)
        for index, item in enumerate(job.results)
    }
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
        by_index[index] = normalize_result(result)
    job.results = [by_index[index] for index in sorted(by_index)]
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
        summary=summarize(job.results),
        download_url=f"/api/jobs/{job.id}/download" if is_done else None,
        download_name=verification_filename(job) if is_done else None,
        queue_position=job_store.queue_position(job.id),
        stop_on_deliverable=job.stop_on_deliverable,
        access_token=job.guest_token,
    )


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/workers/tencent-qq/claim")
async def claim_tencent_qq_job(
    token: Annotated[str | None, Header(alias="X-Verigo-Worker-Token")] = None,
    worker_id: Annotated[str | None, Header(alias="X-Verigo-Worker-Id")] = None,
    wait_seconds: int = Query(default=20, ge=0, le=25),
) -> dict[str, object]:
    require_tencent_worker(token)
    worker_name = (worker_id or "").strip()
    if not worker_name or len(worker_name) > 128:
        raise HTTPException(status_code=422, detail="腾讯 QQ 验证节点标识无效")
    deadline = time.monotonic() + wait_seconds
    while True:
        job = job_store.claim_next(worker_name, execution_target="tencent_qq")
        if job is not None:
            return {
                "job": {
                    "id": job.id,
                    "emails": job.emails,
                    "worker_count": min(job.worker_count, 4),
                    "stop_on_deliverable": job.stop_on_deliverable,
                }
            }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"job": None}
        await asyncio.sleep(min(0.25, remaining))


@router.post("/workers/tencent-qq/jobs/{job_id}/heartbeat")
def heartbeat_tencent_qq_job(
    job_id: str,
    token: Annotated[str | None, Header(alias="X-Verigo-Worker-Token")] = None,
    worker_id: Annotated[str | None, Header(alias="X-Verigo-Worker-Id")] = None,
) -> dict[str, object]:
    require_tencent_worker(token)
    job = require_job(job_id)
    if job.execution_target != "tencent_qq":
        raise HTTPException(status_code=409, detail="不是腾讯 QQ 验证节点任务")
    if job.status == "stopped":
        return {"status": "stopped", "stop_requested": True}
    job = require_tencent_job(job_id, (worker_id or "").strip())
    job_store.heartbeat(job)
    return {"status": job.status, "stop_requested": False}


@router.post("/workers/tencent-qq/jobs/{job_id}/results")
def report_tencent_qq_results(
    job_id: str,
    payload: WorkerResultsRequest,
    token: Annotated[str | None, Header(alias="X-Verigo-Worker-Token")] = None,
    worker_id: Annotated[str | None, Header(alias="X-Verigo-Worker-Id")] = None,
) -> dict[str, object]:
    require_tencent_worker(token)
    job = require_job(job_id)
    if job.execution_target != "tencent_qq":
        raise HTTPException(status_code=409, detail="不是腾讯 QQ 验证节点任务")
    if job.status == "stopped":
        return {"status": "stopped", "stop_requested": True}
    job = require_tencent_job(job_id, (worker_id or "").strip())
    merge_worker_results(job, payload.results)
    job_store.persist(job)
    job_store.heartbeat(job)
    return {"status": job.status, "stop_requested": False, "completed": len(job.results)}


@router.post("/workers/tencent-qq/jobs/{job_id}/complete", response_model=JobResponse)
def complete_tencent_qq_job(
    job_id: str,
    payload: WorkerResultsRequest,
    token: Annotated[str | None, Header(alias="X-Verigo-Worker-Token")] = None,
    worker_id: Annotated[str | None, Header(alias="X-Verigo-Worker-Id")] = None,
) -> JobResponse:
    require_tencent_worker(token)
    job = require_job(job_id)
    if job.execution_target != "tencent_qq":
        raise HTTPException(status_code=409, detail="不是腾讯 QQ 验证节点任务")
    if job.status == "stopped":
        return serialize_job(job)
    job = require_tencent_job(job_id, (worker_id or "").strip())
    merge_worker_results(job, payload.results)
    job_store.cache_results(job.results)
    job_store.record_catch_all(job)
    job.finished_at = utc_now()
    write_csv(job)
    job.status = "completed"
    job_store.persist(job)
    return serialize_job(job)


@router.post("/workers/tencent-qq/jobs/{job_id}/fail", response_model=JobResponse)
def fail_tencent_qq_job(
    job_id: str,
    payload: WorkerFailureRequest,
    token: Annotated[str | None, Header(alias="X-Verigo-Worker-Token")] = None,
    worker_id: Annotated[str | None, Header(alias="X-Verigo-Worker-Id")] = None,
) -> JobResponse:
    require_tencent_worker(token)
    job = require_job(job_id)
    if job.execution_target != "tencent_qq":
        raise HTTPException(status_code=409, detail="不是腾讯 QQ 验证节点任务")
    if job.status == "stopped":
        return serialize_job(job)
    job = require_tencent_job(job_id, (worker_id or "").strip())
    job.error = f"腾讯 QQ 验证节点失败: {payload.error}"
    job.status = "failed"
    job.finished_at = utc_now()
    job_store.persist(job)
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


@router.post("/discovery/candidates", response_model=DiscoveryResponse)
def discovery_candidates(
    payload: DiscoveryRequest,
    _: Annotated[User, Depends(require_user)],
) -> DiscoveryResponse:
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
        job = verification_tasks.submit(
            candidates,
            worker_count=4,
            owner_id=user.id,
            stop_on_deliverable=True,
            job_id=uuid.uuid4().hex[:12],
            execution_target=tencent_qq_target(candidates),
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
    if len(emails) > job_limit:
        raise HTTPException(status_code=422, detail=f"单次最多 {job_limit} 个邮箱")
    job_id = uuid.uuid4().hex[:12]
    charge_reference = f"verification:{job_id}"
    try:
        auth_store.consume_credits(user.id, len(emails), charge_reference)
        job = verification_tasks.submit(
            emails,
            payload.worker_count,
            owner_id=user.id,
            stop_on_deliverable=payload.stop_on_deliverable,
            job_id=job_id,
            execution_target=tencent_qq_target(emails),
        )
    except RuntimeError as exc:
        auth_store.refund_credits(user.id, len(emails), charge_reference)
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
    try:
        metrics_store.reserve_free_single(
            request_network_hash(request), settings.anonymous_free_single_daily_limit
        )
        job = verification_tasks.submit(
            emails,
            worker_count=1,
            owner_id=user.id if user else None,
            job_id=uuid.uuid4().hex[:12],
            execution_target=tencent_qq_target(emails),
        )
    except RuntimeError as exc:
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
        emails = extract_emails(file.filename or "", data, settings.max_emails_per_job)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not emails:
        raise HTTPException(status_code=422, detail="文件中没有识别到邮箱地址")
    return ImportResponse(count=len(emails), emails=emails)
