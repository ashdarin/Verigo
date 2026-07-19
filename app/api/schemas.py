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


class SingleVerificationRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class JobSummary(BaseModel):
    total: int
    valid: int
    deliverable: int
    undeliverable: int
    unknown: int
    catch_all: int


class JobResponse(BaseModel):
    id: str
    status: Literal["queued", "running", "completed", "failed", "stopped"]
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
    qq_slow: bool = False
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


class AdminCreditGrantRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    credits: int = Field(ge=1, le=1_000_000)
    note: str = Field(default="", max_length=200)
    amount_fen: int | None = Field(default=None, ge=0, le=100_000_000)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.strip().lower()


class AdminCreditAdjustmentResponse(BaseModel):
    email: str
    delta: int
    credits: int
    paid_credits: int
    reference: str
    created_at: str


class NotificationResponse(BaseModel):
    id: str
    kind: str
    title: str
    body: str
    created_at: str
    read_at: str | None


class NotificationListResponse(BaseModel):
    items: list[NotificationResponse]
    unread_count: int


class WorkerResultsRequest(BaseModel):
    results: list[dict[str, Any]] = Field(default_factory=list, max_length=5000)


class WorkerFailureRequest(BaseModel):
    error: str = Field(min_length=1, max_length=500)
