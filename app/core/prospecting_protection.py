from __future__ import annotations

from typing import Any

from app.core.result_retry import is_recipient_mailbox_full, smtp_status_code


_RECIPIENT_SPECIFIC_MARKERS = (
    "5.1.1", "user unknown", "unknown user", "no such user", "mailbox unavailable",
    "recipient address rejected", "invalid recipient", "does not exist",
)
_ENUMERATION_MARKERS = (
    "anti-enumerat", "anti enumerat", "enumeration", "directory harvest",
    "too many invalid recipient", "too many recipients", "recipient verification",
    "verification not permitted", "verification not allowed",
)


def is_confirmed_smtp_sample(result: dict[str, Any]) -> bool:
    """A control sample must be a real SMTP 250, never a cached or catch-all result."""
    checks = result.get("checks")
    return bool(
        smtp_status_code(result) == "250"
        and result.get("deliverable") is True
        and result.get("domain_type") != "catch-all"
        and isinstance(checks, dict)
        and checks.get("smtp") is True
    )


def is_suspicious_recipient_rejection(result: dict[str, Any]) -> bool:
    """Return true only for a generic recipient refusal worth control probing."""
    if is_recipient_mailbox_full(result) or smtp_status_code(result) != "550":
        return False
    detail = " ".join(
        str(result.get(field) or "")
        for field in ("smtp_raw_result", "smtp_result", "message", "failure_reason")
    ).lower()
    if any(marker in detail for marker in _ENUMERATION_MARKERS):
        return True
    return not any(marker in detail for marker in _RECIPIENT_SPECIFIC_MARKERS)


def control_sample_rejected(result: dict[str, Any]) -> bool:
    """The decisive anti-enumeration signal: a previously SMTP-250 address becomes 550."""
    return smtp_status_code(result) == "550"
