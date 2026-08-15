from __future__ import annotations

import re
from typing import Any

from app.core.smtp_retry_policy import apply_retry_plan


RETRY_NEVER = "never"
RETRY_DELAYED = "delayed"
RETRY_GREYLIST = "greylist"

_RETRY_POLICIES = frozenset({RETRY_NEVER, RETRY_DELAYED, RETRY_GREYLIST})
_TERMINAL_REASONS = frozenset({
    "format_invalid",
    "domain_nxdomain",
    "mx_missing",
    "unsupported",
    "smtp_permanent",
    "mailbox_full",
    "stopped",
})
_DELAYED_REASONS = frozenset({
    "dns_transient",
    "smtp_timeout",
    "smtp_connection",
    "smtp_temporary",
    "smtp_receiver_throttled",
    "smtp_infrastructure",
    "provider_throttled",
})


def smtp_code(result: dict[str, Any]) -> str | None:
    """Read a structured SMTP code, falling back only for legacy results."""
    value = result.get("smtp_code")
    if value is not None:
        code = str(value)
        if re.fullmatch(r"[245]\d{2}", code):
            return code
    detail = " ".join(
        str(result.get(field) or "") for field in ("smtp_raw_result", "smtp_result", "message")
    )
    match = re.search(r"\b([245]\d{2})\b", detail)
    return match.group(1) if match else None


def apply_outcome(
    result: dict[str, Any],
    *,
    stage: str,
    reason: str,
    retry_policy: str,
    code: str | int | None = None,
) -> dict[str, Any]:
    """Attach machine-readable verification semantics without changing display text."""
    if retry_policy not in _RETRY_POLICIES:
        raise ValueError(f"Unsupported retry policy: {retry_policy}")
    result["failure_stage"] = stage
    result["failure_reason"] = reason
    result["retry_policy"] = retry_policy
    if code is not None:
        result["smtp_code"] = str(code)
    if retry_policy != RETRY_NEVER:
        apply_retry_plan(result)
    return result


def ensure_outcome(result: dict[str, Any]) -> dict[str, Any]:
    """Classify legacy results conservatively until every producer is upgraded."""
    policy = str(result.get("retry_policy") or "")
    if policy in _RETRY_POLICIES:
        if policy != RETRY_NEVER:
            apply_retry_plan(result)
        return result

    reason = str(result.get("failure_reason") or "")
    stage = str(result.get("failure_stage") or "")
    if reason in _TERMINAL_REASONS:
        return apply_outcome(result, stage=stage or "verification", reason=reason, retry_policy=RETRY_NEVER)
    if reason in _DELAYED_REASONS:
        return apply_outcome(result, stage=stage or "verification", reason=reason, retry_policy=RETRY_DELAYED)
    if reason == "smtp_greylist":
        return apply_outcome(result, stage=stage or "smtp", reason=reason, retry_policy=RETRY_GREYLIST)

    checks = result.get("checks")
    if isinstance(checks, dict):
        if checks.get("domain") is False:
            return apply_outcome(
                result, stage="dns", reason="domain_nxdomain", retry_policy=RETRY_NEVER
            )
        if checks.get("domain") is True and checks.get("mx") is False:
            return apply_outcome(
                result, stage="mx", reason="mx_missing", retry_policy=RETRY_NEVER
            )

    code = smtp_code(result)
    detail = " ".join(
        str(result.get(field) or "") for field in ("smtp_raw_result", "smtp_result", "message")
    ).lower()
    if result.get("delivery_block_reason") == "mailbox_full" or re.search(r"\b[45]\.2\.2\b", detail):
        return apply_outcome(
            result, stage="smtp", reason="mailbox_full", retry_policy=RETRY_NEVER, code=code
        )
    if code and code.startswith("5"):
        return apply_outcome(
            result, stage="smtp", reason="smtp_permanent", retry_policy=RETRY_NEVER, code=code
        )
    plan = apply_retry_plan(result)
    if plan.retry_class == "greylist":
        return apply_outcome(
            result, stage="smtp", reason="smtp_greylist", retry_policy=RETRY_GREYLIST, code=code
        )
    if plan.retry_class == "receiver_throttled":
        return apply_outcome(
            result, stage="smtp", reason="smtp_receiver_throttled", retry_policy=RETRY_DELAYED, code=code
        )
    if code == "450" and any(marker in detail for marker in ("greylist", "greylisted", "postgrey", "灰名单")):
        return apply_outcome(
            result, stage="smtp", reason="smtp_greylist", retry_policy=RETRY_GREYLIST, code=code
        )
    if code and code.startswith("4"):
        return apply_outcome(
            result, stage="smtp", reason="smtp_temporary", retry_policy=RETRY_DELAYED, code=code
        )

    # Legacy text is only a compatibility path. Do not treat the word "SMTP"
    # itself as a failure signal: terminal DNS/MX messages also mention it.
    if any(marker in detail for marker in ("timeout", "timed out", "超时")):
        return apply_outcome(
            result, stage="smtp", reason="smtp_timeout", retry_policy=RETRY_DELAYED
        )
    if any(marker in detail for marker in ("connection", "connect", "连接被", "连接失败", "连接排队")):
        return apply_outcome(
            result, stage="smtp", reason="smtp_connection", retry_policy=RETRY_DELAYED
        )
    if "smtp暂时无法确认" in detail or "smtp 暂时无法确认" in detail:
        return apply_outcome(
            result, stage="smtp", reason="smtp_temporary", retry_policy=RETRY_DELAYED
        )
    return apply_outcome(result, stage=stage or "verification", reason=reason or "terminal", retry_policy=RETRY_NEVER)


def retry_policy(result: dict[str, Any]) -> str:
    return str(ensure_outcome(result)["retry_policy"])


def is_retryable(result: dict[str, Any]) -> bool:
    return retry_policy(result) in {RETRY_DELAYED, RETRY_GREYLIST}


def is_greylist(result: dict[str, Any]) -> bool:
    return retry_policy(result) == RETRY_GREYLIST
