from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import settings
from app.core.prospecting import normalize_country, normalize_person_pattern


class CreateJobRequest(BaseModel):
    emails: list[str] = Field(min_length=1)
    worker_count: int = Field(default=2, ge=1, le=settings.max_workers_per_job)
    stop_on_deliverable: bool = False

    @field_validator("emails")
    @classmethod
    def check_job_size(cls, value: list[str]) -> list[str]:
        if (
            settings.max_emails_per_job > 0
            and len(value) > settings.max_emails_per_job
        ):
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
    retry_at: str | None = None
    stop_on_deliverable: bool = False
    qq_slow: bool = False
    review_updated: bool = False
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


class ProspectingRunRequest(BaseModel):
    domain: str = Field(min_length=3, max_length=253)
    country: str = Field(min_length=2, max_length=8)
    email_pattern: str | None = Field(default=None, max_length=32)
    known_first_name: str | None = Field(default=None, max_length=64)
    known_last_name: str | None = Field(default=None, max_length=64)
    known_email: str | None = Field(default=None, max_length=254)

    @field_validator("country")
    @classmethod
    def check_country(cls, value: str) -> str:
        return normalize_country(value)

    @field_validator("email_pattern")
    @classmethod
    def check_email_pattern(cls, value: str | None) -> str | None:
        return normalize_person_pattern(value)

    @model_validator(mode="after")
    def check_known_contact(self) -> "ProspectingRunRequest":
        values = (self.known_first_name, self.known_last_name, self.known_email)
        if any(value is not None and value.strip() for value in values) and not all(
            value is not None and value.strip() for value in values
        ):
            raise ValueError("Provide the known contact's first name, last name, and email together")
        return self


class ProspectingRunResponse(BaseModel):
    id: str
    domain: str
    country: str
    requested_pattern: str | None
    verification_job_id: str
    status: Literal["queued", "running", "completed", "failed", "stopped"]
    created_at: str
    total: int
    completed: int
    progress: float
    error: str | None
    profile_patterns: list[str]
    summary: dict[str, int]
    result_total: int
    saved_count: int
    protection: dict[str, Any]


class ProspectingRunPageResponse(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[ProspectingRunResponse]


class ProspectingResultsResponse(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[dict[str, Any]]


class SavedProspectingContactsResponse(BaseModel):
    workspace_total: int
    total: int
    items: list[dict[str, Any]]
    domains: list[dict[str, Any]]
    offset: int
    limit: int
    domain_total: int
    domain_offset: int
    domain_limit: int


class ProspectingContactUpdateRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    favorite: bool | None = None
    tags: list[str] | None = Field(default=None, max_length=20)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        tags = list(dict.fromkeys(item.strip()[:40] for item in value if item.strip()))
        if len(tags) != len(value):
            raise ValueError("Tags must be non-empty and unique")
        return tags


class ProspectingCompanyImportResponse(BaseModel):
    import_id: str
    imported: int


class ProspectingCompanyPageResponse(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[dict[str, Any]]


class ProspectingCompanyUpdateRequest(BaseModel):
    domain: str | None = Field(default=None, max_length=253)
    country: str | None = Field(default=None, max_length=8)
    selected: bool | None = None

    @field_validator("country")
    @classmethod
    def check_country(cls, value: str | None) -> str | None:
        return normalize_country(value) if value and value.strip() else None


class ProspectingCompanyDiscoverRequest(BaseModel):
    company_ids: list[str] = Field(min_length=1, max_length=25)
    country: str

    @field_validator("country")
    @classmethod
    def check_country(cls, value: str) -> str:
        return normalize_country(value)


class PaymentOrderRequest(BaseModel):
    packages: int = Field(ge=1, le=1000)


class PaymentOrderResponse(BaseModel):
    id: str
    credits: int
    amount_fen: int
    status: str
    checkout_url: str | None = None
    payment_enabled: bool = False


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
    target_job_id: str | None
    target_email: str | None
    target_result_index: int | None


class NotificationListResponse(BaseModel):
    items: list[NotificationResponse]
    unread_count: int
    total: int
    offset: int
    limit: int


class WorkerResultsRequest(BaseModel):
    results: list[dict[str, Any]] = Field(default_factory=list, max_length=5000)
    control_probes: list[dict[str, Any]] = Field(default_factory=list, max_length=4)
    lease_id: str | None = Field(default=None, min_length=8, max_length=64)


class WorkerFailureRequest(BaseModel):
    error: str = Field(min_length=1, max_length=500)
    lease_id: str = Field(min_length=8, max_length=64)
