"""Policy for one bounded SMTP recheck through a different worker route.

Cross-route checks are diagnostic evidence. They are deliberately excluded for
QQ and Microsoft receivers, mailbox-full responses, greylisting, and receiver
throttling. A second route is allowed only for an address-level temporary
failure or a verifier-side infrastructure failure.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

from app.config import settings
from app.core.provider_policy import is_qq_email
from app.core.result_retry import is_recipient_mailbox_full
from app.core.smtp_retry_policy import retry_plan, smtp_status_code
from app.core.verification_outcome import smtp_code


ROUTE_SAME_TARGET = "same_target"
ROUTE_ALTERNATE = "alternate_route"
ROUTE_TERMINAL = "terminal"

QQ_PROVIDER_KEYS = frozenset({"qq", "tencent"})
MICROSOFT_PROVIDER_KEYS = frozenset({"microsoft", "outlook", "exchange_online"})
INFRASTRUCTURE_CLASSES = frozenset({"infrastructure"})


@dataclass(frozen=True)
class CrossRouteDecision:
    eligible: bool
    reason: str
    provider_key: str
    alternate_target: str | None = None


def _domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].strip().lower().rstrip(".") if "@" in email else ""


def _mx_host(result: dict[str, Any]) -> str:
    raw_hosts = result.get("mx_records")
    if isinstance(raw_hosts, (list, tuple)):
        for raw_host in raw_hosts:
            host = str(raw_host).strip().lower().rstrip(".")
            if host:
                return host
    return ""


def provider_key(email: str, result: dict[str, Any] | None = None) -> str:
    """Return a stable receiver bucket using MX evidence before the domain."""
    result = result or {}
    host = _mx_host(result)
    domain = _domain(email)
    if domain in {"qq.com", "vip.qq.com", "foxmail.com"} or host.endswith((".qq.com", ".foxmail.com")):
        return "qq"
    if domain in {"outlook.com", "hotmail.com", "live.com", "msn.com"} or host.endswith(
        ".protection.outlook.com"
    ):
        return "microsoft"
    if domain in {"gmail.com", "googlemail.com"} or host.endswith((".google.com", ".googlemail.com")):
        return "gmail"
    if host:
        return f"mx:{host}"
    return f"domain:{domain}" if domain else "unknown"


def _response_signature(result: dict[str, Any]) -> str:
    detail = " ".join(
        str(result.get(field) or "")
        for field in ("smtp_raw_result", "smtp_result", "message")
    ).lower()
    detail = re.sub(r"\s+", " ", detail).strip()
    code = smtp_code(result) or smtp_status_code(result) or ""
    return f"{code}:{detail[:160]}"


def provider_pressure_keys(results: Iterable[dict[str, Any]]) -> set[str]:
    """Find receiver buckets showing a short-window batch pressure pattern.

    The caller supplies one completion batch, which is the strongest signal
    available without another database round trip. The scheduler's existing
    MX cooldown remains authoritative for longer windows.
    """
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        email = str(result.get("email") or "")
        if not email:
            continue
        code = smtp_code(result) or smtp_status_code(result)
        if code or result.get("deliverable") is not None:
            grouped[provider_key(email, result)].append(result)

    pressure: set[str] = set()
    for key, group in grouped.items():
        if len(group) < settings.smtp_cross_route_pressure_min_samples:
            continue
        temporary = sum(
            1
            for item in group
            if (smtp_code(item) or smtp_status_code(item) or "").startswith("4")
        )
        signatures = Counter(
            _response_signature(item)
            for item in group
            if (smtp_code(item) or smtp_status_code(item) or "").startswith("4")
        )
        rate = temporary / len(group)
        repeated_response = max(signatures.values(), default=0) >= 3
        if rate >= settings.smtp_cross_route_pressure_4xx_rate or repeated_response:
            pressure.add(key)
    return pressure


def alternate_target(source_target: str) -> str | None:
    """Select a route with a different egress from the source worker."""
    configured = settings.smtp_cross_route_target.strip().lower()
    if configured not in {"local", "gmail", "codearts", "cloudstudio_domestic"}:
        configured = "local"
    if source_target == configured:
        return None
    return configured


def decision_for(
    email: str,
    result: dict[str, Any],
    *,
    source_target: str,
    pressure_keys: set[str] | None = None,
    allow_alternate: bool = True,
) -> CrossRouteDecision:
    key = provider_key(email, result)
    if not settings.smtp_cross_route_enabled and not settings.smtp_cross_route_shadow_mode:
        return CrossRouteDecision(False, "feature_disabled", key)
    if not allow_alternate:
        return CrossRouteDecision(False, "route_already_checked", key)
    if source_target == "tencent_qq":
        return CrossRouteDecision(False, "qq_source_excluded", key)
    if is_qq_email(email) or key in QQ_PROVIDER_KEYS:
        return CrossRouteDecision(False, "qq_excluded", key)
    if key in MICROSOFT_PROVIDER_KEYS:
        return CrossRouteDecision(False, "microsoft_excluded", key)
    if int(result.get("cross_route_attempts") or 0) >= settings.smtp_cross_route_max_per_email:
        return CrossRouteDecision(False, "attempt_limit", key)
    if is_recipient_mailbox_full(result):
        return CrossRouteDecision(False, "mailbox_full", key)
    plan = retry_plan(result)
    if plan.retry_class == "greylist":
        return CrossRouteDecision(False, "greylist", key)
    if plan.retry_class == "receiver_throttled":
        return CrossRouteDecision(False, "receiver_throttled", key)
    if pressure_keys and key in pressure_keys:
        return CrossRouteDecision(False, "provider_pressure", key)
    target = alternate_target(source_target)
    if target is None:
        return CrossRouteDecision(False, "no_distinct_route", key)
    if plan.retry_class in INFRASTRUCTURE_CLASSES:
        return CrossRouteDecision(True, "infrastructure_failure", key, target)
    code = smtp_code(result) or smtp_status_code(result)
    if plan.retry_class == "temporary" and code and code.startswith("4"):
        return CrossRouteDecision(True, "isolated_temporary_4xx", key, target)
    return CrossRouteDecision(False, "not_eligible", key)


def mark_decision(result: dict[str, Any], decision: CrossRouteDecision, *, shadow: bool) -> None:
    """Attach machine-readable evidence without changing user-facing SMTP text."""
    result["cross_route_provider"] = decision.provider_key
    result["cross_route_decision"] = decision.reason
    result["cross_route_shadow"] = bool(shadow)
    if decision.alternate_target:
        result["cross_route_target"] = decision.alternate_target
