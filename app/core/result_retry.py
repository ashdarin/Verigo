from __future__ import annotations

import re
from typing import Any


GREYLIST_MARKERS = ("greylist", "greylisted", "postgrey", "灰名单")
MAILBOX_FULL_MARKERS = (
    "mailbox full",
    "mailbox is full",
    "mailbox quota",
    "over quota",
    "quota exceeded",
    "quota has been exceeded",
    "storage quota",
    "storage full",
    "insufficient storage",
    "user is over quota",
    "邮箱已满",
    "邮箱容量已满",
    "存储空间已满",
    "配额已满",
)


def smtp_status_code(result: dict[str, Any]) -> str | None:
    """Extract the first SMTP completion status from a verification result."""
    detail = " ".join(
        str(result.get(field) or "") for field in ("smtp_result", "message")
    )
    match = re.search(r"\b([245]\d{2})\b", detail)
    return match.group(1) if match else None


def smtp_temporary_status(result: dict[str, Any]) -> str | None:
    """Return a temporary SMTP status code without deciding its retry policy."""
    code = smtp_status_code(result)
    return code if code and code.startswith("4") else None


def is_recipient_mailbox_full(result: dict[str, Any]) -> bool:
    """Recognize explicit recipient storage/quota failures, including SMTP 4.2.2."""
    if result.get("delivery_block_reason") == "mailbox_full":
        return True
    detail = " ".join(
        str(result.get(field) or "")
        for field in ("smtp_raw_result", "smtp_result", "message")
    ).lower()
    return bool(
        re.search(r"\b[45]\.2\.2\b", detail)
        or any(marker in detail for marker in MAILBOX_FULL_MARKERS)
    )


def is_retryable_smtp_result(result: dict[str, Any]) -> bool:
    """Retry transient SMTP responses, never full mailboxes or permanent failures."""
    if is_recipient_mailbox_full(result):
        return False
    if smtp_temporary_status(result):
        return True
    if smtp_permanent_status(result):
        return False
    detail = " ".join(
        str(result.get(field) or "") for field in ("smtp_result", "message")
    ).lower()
    return any(marker in detail for marker in (
        "timeout", "timed out", "connection", "connect", "smtp", "超时", "连接",
    ))


def smtp_permanent_status(result: dict[str, Any]) -> str | None:
    """Return a permanent SMTP failure code that must not be retried."""
    code = smtp_status_code(result)
    return code if code and code.startswith("5") else None


def is_smtp_greylisted(result: dict[str, Any]) -> bool:
    detail = " ".join(
        str(result.get(field) or "") for field in ("smtp_result", "message")
    ).lower()
    return smtp_temporary_status(result) == "450" and any(
        marker in detail for marker in GREYLIST_MARKERS
    )


def is_temporary_smtp_452(result: dict[str, Any]) -> bool:
    """Backward-compatible name retained for external callers."""
    return smtp_temporary_status(result) == "452" and is_retryable_smtp_result(result)
