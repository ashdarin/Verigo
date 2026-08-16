"""Confidence-aware reuse policy for completed email verification results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.result_retry import is_recipient_mailbox_full, smtp_permanent_status


@dataclass(frozen=True)
class CacheDecision:
    outcome_class: str
    fresh_for: timedelta
    stale_for: timedelta


def _utc(value: datetime | None = None) -> datetime:
    now = value or datetime.now(timezone.utc)
    return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


def sanitize_cached_result(result: dict[str, Any]) -> dict[str, Any]:
    payload = dict(result)
    for key in (
        "original_index", "progress_state", "cache_hit", "cache_age_seconds",
        "cache_outcome", "_cache_refresh_due",
    ):
        payload.pop(key, None)
    return payload


def is_cache_excluded(result: dict[str, Any]) -> bool:
    """Catch-all evidence is domain-level and must never become an email verdict."""
    return bool(
        result.get("domain_type") == "catch-all"
        or result.get("is_catch_all") is True
        or result.get("failure_reason") == "catch_all_conflict"
    )


def cache_decision(
    result: dict[str, Any],
    *,
    confirmation_count: int = 0,
    first_confirmed_at: datetime | None = None,
    now: datetime | None = None,
    deliverable_first_days: int = 7,
    deliverable_repeat_days: int = 14,
    deliverable_stable_days: int = 30,
    permanent_days: int = 3,
    mailbox_full_hours: int = 2,
    stale_days: int = 90,
) -> CacheDecision | None:
    """Return a TTL only for a result that is safe to reuse as a verdict."""
    if not result.get("email"):
        return None
    if is_cache_excluded(result):
        return None

    if is_recipient_mailbox_full(result):
        return CacheDecision(
            "mailbox_full",
            timedelta(hours=max(1, mailbox_full_hours)),
            timedelta(days=1),
        )

    if result.get("deliverable") is True:
        fresh_days = max(1, deliverable_first_days)
        first = _utc(first_confirmed_at) if first_confirmed_at else None
        age = _utc(now) - first if first else timedelta(0)
        if confirmation_count >= 2 and age >= timedelta(days=7):
            fresh_days = max(fresh_days, deliverable_stable_days)
        elif confirmation_count >= 2 and age >= timedelta(days=1):
            fresh_days = max(fresh_days, deliverable_repeat_days)
        return CacheDecision(
            "deliverable", timedelta(days=fresh_days), timedelta(days=max(fresh_days, stale_days)),
        )

    method = str(result.get("verification_method") or "").lower()
    strategy = str(result.get("strategy") or "").lower()
    microsoft_api = method in {"microsoft_api", "outlook 账号验证"} or strategy == "outlook_http"
    permanent = (
        result.get("deliverable") is False
        and (
            result.get("failure_reason") == "smtp_permanent"
            or smtp_permanent_status(result) is not None
            or microsoft_api
        )
    )
    if permanent:
        return CacheDecision(
            "permanent_invalid",
            timedelta(days=max(1, permanent_days)),
            timedelta(days=max(permanent_days, stale_days)),
        )
    return None
