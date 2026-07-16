from __future__ import annotations

import hmac
import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from app.api.auth import optional_user, require_user
from app.api.schemas import (
    CreateJobRequest,
    DiscoveryRequest,
    DiscoveryResponse,
    ImportResponse,
    JobResponse,
    PaymentOrderRequest,
    PaymentOrderResponse,
    ResultsResponse,
)
from app.config import settings
from app.core.imports import extract_emails
from app.core.discovery import candidate_emails
from app.core.security import token_hash
from app.db.auth import User, auth_store
from app.db.jobs import Job, job_store
from app.tasks.verification import (
    clean_emails,
    job_progress,
    normalize_result,
    summarize,
    verification_filename,
    verification_tasks,
)


router = APIRouter(prefix="/api")


def require_job(job_id: str) -> Job:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或服务已重启")
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
    is_done = job.status == "completed"
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


@router.post("/jobs", response_model=JobResponse, status_code=202)
def create_job(
    payload: CreateJobRequest,
    user: Annotated[User, Depends(require_user)],
) -> JobResponse:
    emails = clean_emails(payload.emails)
    if not emails:
        raise HTTPException(status_code=422, detail="邮箱包含空格、非 ASCII 或非法字符")
    job_limit = settings.max_emails_per_job
    if len(emails) > job_limit:
        raise HTTPException(status_code=422, detail=f"单次最多 {job_limit} 个邮箱")
    job_id = uuid.uuid4().hex[:12]
    try:
        auth_store.consume_credits(user.id, len(emails), f"verification:{job_id}")
        job = verification_tasks.submit(
            emails,
            payload.worker_count,
            owner_id=user.id,
            stop_on_deliverable=payload.stop_on_deliverable,
            job_id=job_id,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
    if job.status != "completed" or job.csv_path is None or not job.csv_path.exists():
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
