from __future__ import annotations

import re
from typing import Any


TEMPORARY_SMTP_CODES = frozenset({"421", "450", "451", "452"})
GREYLIST_MARKERS = ("greylist", "greylisted", "postgrey", "灰名单")


def smtp_temporary_status(result: dict[str, Any]) -> str | None:
    """Return the temporary SMTP status code, when a result is retryable."""
    detail = " ".join(
        str(result.get(field) or "") for field in ("smtp_result", "message")
    )
    match = re.search(r"\b([245]\d{2})\b", detail)
    return match.group(1) if match and match.group(1) in TEMPORARY_SMTP_CODES else None


def is_smtp_greylisted(result: dict[str, Any]) -> bool:
    detail = " ".join(
        str(result.get(field) or "") for field in ("smtp_result", "message")
    ).lower()
    return smtp_temporary_status(result) == "450" and any(
        marker in detail for marker in GREYLIST_MARKERS
    )


def is_temporary_smtp_452(result: dict[str, Any]) -> bool:
    """Backward-compatible name retained for external callers."""
    return smtp_temporary_status(result) == "452"
