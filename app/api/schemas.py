from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.config import settings


class CreateJobRequest(BaseModel):
    emails: list[str] = Field(min_length=1)
    worker_count: int = Field(default=2, ge=1, le=settings.max_workers_per_job)
    stop_on_deliverable: bool = False

    @field_validator("emails")
    @classmethod
    def check_job_size(cls, value: list[str]) -> list[str]:
        if len(value) > settings.max_emails_per_job:
            raise ValueError(f"单个任务最多 {settings.max_emails_per_job} 个邮箱")
        return value


class JobSummary(BaseModel):
    total: int
    valid: int
    deliverable: int
    undeliverable: int
    unknown: int
    catch_all: int


class JobResponse(BaseModel):
    id: str
    status: Literal["queued", "running", "completed", "failed"]
    worker_count: int
    completed: int
    total: int
    progress: float
    created_at: str
    started_at: str | None
    finished_at: str | None
    error: str | None
    summary: JobSummary | None
    download_url: str | None
    download_name: str | None = None
    queue_position: int | None = None
    stop_on_deliverable: bool = False
    access_token: str | None = None


class ResultsResponse(BaseModel):
    total: int
    available: int
    offset: int
    limit: int
    items: list[dict[str, Any]]


class ImportResponse(BaseModel):
    count: int
    emails: list[str]


class DiscoveryRequest(BaseModel):
    first_name: str = Field(min_length=1, max_length=64)
    last_name: str = Field(min_length=1, max_length=64)
    domain: str = Field(min_length=3, max_length=253)


class DiscoveryResponse(BaseModel):
    candidates: list[str]


class PaymentOrderRequest(BaseModel):
    packages: int = Field(ge=1, le=1000)


class PaymentOrderResponse(BaseModel):
    id: str
    credits: int
    amount_fen: int
    status: str
