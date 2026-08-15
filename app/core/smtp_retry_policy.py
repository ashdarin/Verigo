from __future__ import annotations

"""Classify SMTP transient responses into bounded, receiver-friendly reviews.

SMTP 4xx responses do not share a single meaning.  This policy deliberately
keeps greylisting, receiver throttling, and verifier infrastructure failures
separate so retry work can be delayed without repeatedly probing a receiver.
"""

from dataclasses import dataclass
import re
from typing import Any


GREYLIST_MARKERS = (
    "greylist",
    "greylisted",
    "postgrey",
    "greylisting",
)
THROTTLE_MARKERS = (
    "rate limit",
    "rate limited",
    "too many",
    "throttl",
    "try again later",
    "temporarily deferred",
    "temporarily unavailable",
    "too many connections",
    "resource temporarily unavailable",
)
INFRASTRUCTURE_REASONS = frozenset({
    "dns_transient",
    "smtp_timeout",
    "smtp_connection",
})


@dataclass(frozen=True)
class SMTPRetryPlan:
    retry_class: str
    reason: str
    delays_seconds: tuple[int, ...]
    receiver_cooldown_seconds: int

    @property
    def max_attempts(self) -> int:
        return len(self.delays_seconds)

    def delay_for_attempt(self, attempt: int) -> int | None:
        """Return the delay before a one-based background recheck attempt."""
        if attempt < 1 or attempt > self.max_attempts:
            return None
        return self.delays_seconds[attempt - 1]


NO_RETRY = SMTPRetryPlan("none", "terminal", (), 0)
# Greylisting needs a stable retry tuple and enough time to age.  The second
# recheck handles receivers whose policy windows are closer to one hour.
GREYLIST_RETRY = SMTPRetryPlan("greylist", "smtp_greylist", (15 * 60, 60 * 60), 15 * 60)
# A receiver asking us to slow down is a domain/MX event, never an address
# verdict.  Long spacing prevents a batch from continuously re-applying load.
THROTTLED_RETRY = SMTPRetryPlan("receiver_throttled", "smtp_receiver_throttled", (20 * 60, 60 * 60), 20 * 60)
# RFC 5321 recommends a meaningful delay after a failed destination.  A single
# recheck gives ambiguous address-level 4xx a chance to settle without turning
# an inconclusive response into an invalid-address result.
TEMPORARY_RETRY = SMTPRetryPlan("temporary", "smtp_temporary", (30 * 60,), 5 * 60)
# DNS/socket failures are verifier-side and merit one shorter repair attempt;
# they are not signals to repeatedly probe the recipient address.
INFRASTRUCTURE_RETRY = SMTPRetryPlan("infrastructure", "smtp_infrastructure", (5 * 60,), 0)


def _detail(result: dict[str, Any]) -> str:
    return " ".join(
        str(result.get(field) or "")
        for field in ("smtp_raw_result", "smtp_result", "message")
    ).lower()


def smtp_status_code(result: dict[str, Any]) -> str | None:
    value = result.get("smtp_code")
    if value is not None and re.fullmatch(r"[245]\d{2}", str(value)):
        return str(value)
    match = re.search(r"\b([245]\d{2})\b", _detail(result))
    return match.group(1) if match else None


def retry_plan(result: dict[str, Any]) -> SMTPRetryPlan:
    """Return a reason-specific retry plan without changing the result."""
    if result.get("delivery_block_reason") == "mailbox_full":
        return NO_RETRY
    detail = _detail(result)
    if re.search(r"\b[45]\.2\.2\b", detail) or any(
        marker in detail
        for marker in ("mailbox full", "mailbox quota", "over quota", "quota exceeded", "storage full")
    ):
        return NO_RETRY

    code = smtp_status_code(result)
    if code and code.startswith("5"):
        return NO_RETRY
    if str(result.get("failure_reason") or "") in INFRASTRUCTURE_REASONS:
        return INFRASTRUCTURE_RETRY
    if not (code and code.startswith("4")):
        return NO_RETRY
    if any(marker in detail for marker in GREYLIST_MARKERS):
        return GREYLIST_RETRY
    if code == "421" or any(marker in detail for marker in THROTTLE_MARKERS):
        return THROTTLED_RETRY
    return TEMPORARY_RETRY


def apply_retry_plan(result: dict[str, Any]) -> SMTPRetryPlan:
    """Persist only explanatory metadata; job scheduling remains elsewhere."""
    plan = retry_plan(result)
    result["retry_class"] = plan.retry_class
    result["retry_reason"] = plan.reason
    result["retry_max_attempts"] = plan.max_attempts
    if plan.receiver_cooldown_seconds:
        result["receiver_cooldown_seconds"] = plan.receiver_cooldown_seconds
    else:
        result.pop("receiver_cooldown_seconds", None)
    return plan
