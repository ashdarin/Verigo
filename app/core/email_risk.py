"""Deterministic, non-network email-address risk signals.

The signals in this module are advisory metadata.  They deliberately do not
change the deliverability verdict: an address can be deliverable while still
being a role account, a free mailbox, or behind a secure email gateway.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from app.core.result_retry import is_recipient_mailbox_full


# Exact domains keep classification explainable and avoid guessing from a
# provider-looking substring in a customer's company domain.
DISPOSABLE_DOMAINS = frozenset({
    "10minutemail.com", "dispostable.com", "dropmail.me", "emailondeck.com",
    "getnada.com", "guerrillamail.com", "guerrillamail.net", "mail.tm",
    "maildrop.cc", "mohmal.com", "sharklasers.com", "temp-mail.org",
    "tempail.com", "tempmail.com", "throwawaymail.com", "trashmail.com",
    "yopmail.com",
})

FREE_EMAIL_DOMAINS = frozenset({
    "126.com", "163.com", "aol.com", "gmx.com", "gmx.de", "gmail.com",
    "googlemail.com", "hotmail.com", "hotmail.co.uk", "icloud.com", "live.com",
    "mail.com", "mail.ru", "msn.com", "outlook.com", "outlook.jp", "proton.me",
    "protonmail.com", "qq.com", "sina.com", "sohu.com", "tutanota.com",
    "web.de", "yahoo.com", "yahoo.co.jp", "yahoo.co.uk", "yandex.com",
    "yandex.ru", "zoho.com",
})

ROLE_LOCAL_PARTS = frozenset({
    "abuse", "accounting", "admin", "billing", "careers", "compliance",
    "contact", "customerservice", "feedback", "hello", "help", "hr", "info",
    "inquiries", "legal", "marketing", "media", "office", "orders", "partners",
    "postmaster", "press", "privacy", "sales", "security", "support", "team",
    "webmaster",
})

SEG_SUFFIXES = {
    "proofpoint": ("pphosted.com", "pp-hosted.com", "ppe-hosted.com"),
    "mimecast": ("mimecast.com", "mimecast.co"),
    "barracuda": ("barracudanetworks.com", "barracuda.com"),
}

def _split_address(email: object) -> tuple[str, str]:
    value = "" if email is None else str(email)
    local, separator, domain = value.strip().rpartition("@")
    return (local.lower(), domain.lower().rstrip(".")) if separator else ("", "")


def _signal(detected: bool | None, detail: str | None = None, **extra: Any) -> dict[str, Any]:
    value: dict[str, Any] = {"detected": detected}
    if detail:
        value["detail"] = detail
    value.update(extra)
    return value


def _is_irregular_local_part(local: str) -> tuple[bool, str | None]:
    if not local:
        return False, None
    if re.search(r"([a-z0-9])\1{5,}", local, flags=re.IGNORECASE):
        return True, "repeated character sequence"
    if re.search(r"\d{8,}", local):
        return True, "long numeric sequence"
    if re.search(r"[._-]{2,}", local):
        return True, "repeated separator"
    if len(local) > 64:
        return True, "unusually long local part"
    return False, None


def _has_suspicious_unicode(raw_email: object) -> tuple[bool, str | None]:
    raw = "" if raw_email is None else str(raw_email)
    if any(ord(character) > 127 for character in raw):
        return True, "contains non-ASCII Unicode characters"
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        return True, "contains control characters"
    return False, None


def initial_email_risk_signals(email: object) -> dict[str, dict[str, Any]]:
    """Return address-only signals without DNS or SMTP work."""
    local, domain = _split_address(email)
    irregular, irregular_detail = _is_irregular_local_part(local)
    unicode_found, unicode_detail = _has_suspicious_unicode(email)
    role_local = local.split("+", 1)[0]
    compact_local = re.sub(r"[._-]", "", role_local)
    is_tagged = "+" in local and not local.startswith("+") and not local.endswith("+")
    return {
        "disposable_provider": _signal(
            domain in DISPOSABLE_DOMAINS,
            f"{domain} is a known disposable email provider" if domain in DISPOSABLE_DOMAINS else None,
            domain=domain or None,
        ),
        "free_provider": _signal(
            domain in FREE_EMAIL_DOMAINS,
            f"{domain} is a known free email provider" if domain in FREE_EMAIL_DOMAINS else None,
            domain=domain or None,
        ),
        "role_address": _signal(
            role_local in ROLE_LOCAL_PARTS,
            f"{role_local}@ is a shared role address" if role_local in ROLE_LOCAL_PARTS else None,
            local_part=local or None,
        ),
        "tagged_address": _signal(
            is_tagged,
            "plus-address tag detected" if is_tagged else None,
            local_part=local or None,
        ),
        "mailbox_full": _signal(None),
        "do_not_reply": _signal(
            compact_local in {"noreply", "donotreply"},
            "address is intended not to receive replies" if compact_local in {"noreply", "donotreply"} else None,
            local_part=local or None,
        ),
        "irregular_pattern": _signal(irregular, irregular_detail, local_part=local or None),
        "unicode_or_suspicious_characters": _signal(unicode_found, unicode_detail),
        "secure_email_gateway": _signal(None, providers=[], mx_hosts=[]),
    }


def detect_secure_email_gateway(mx_records: Iterable[object]) -> dict[str, Any]:
    """Classify well-known SEG MX suffixes from records already resolved."""
    hosts = [str(host).lower().rstrip(".") for host in mx_records if str(host).strip()]
    providers = sorted({
        provider
        for host in hosts
        for provider, suffixes in SEG_SUFFIXES.items()
        if any(host == suffix or host.endswith(f".{suffix}") for suffix in suffixes)
    })
    return _signal(
        bool(providers),
        f"MX is protected by {', '.join(providers)}" if providers else None,
        providers=providers,
        mx_hosts=hosts,
    )


def is_mailbox_full(detail: object) -> bool:
    """Use the same quota classifier as retry/final-result handling."""
    return is_recipient_mailbox_full({"smtp_result": detail})


def ensure_email_risk_signals(result: dict[str, Any]) -> dict[str, Any]:
    """Backfill advisory signals for every local and remote result shape.

    The function preserves richer worker-provided fields and lets old cached
    records gain deterministic address metadata when the API serializes them.
    """
    existing = result.get("risk_signals")
    signals = dict(existing) if isinstance(existing, dict) else {}
    defaults = initial_email_risk_signals(result.get("email"))
    for name, signal in defaults.items():
        if not isinstance(signals.get(name), dict):
            signals[name] = signal

    mx_records = result.get("mx_records")
    if isinstance(mx_records, (list, tuple, set)):
        signals["secure_email_gateway"] = detect_secure_email_gateway(mx_records)

    detail = " ".join(str(result.get(name) or "") for name in ("smtp_raw_result", "smtp_result", "message"))
    if is_mailbox_full(detail):
        signals["mailbox_full"] = _signal(True, "SMTP server reported a full mailbox")
    elif not isinstance(signals.get("mailbox_full"), dict):
        signals["mailbox_full"] = _signal(None)

    result["risk_signals"] = signals
    checks = result.get("checks")
    if not isinstance(checks, dict):
        checks = {}
        result["checks"] = checks
    checks.update({
        "disposable": signals["disposable_provider"]["detected"],
        "free_provider": signals["free_provider"]["detected"],
        "role_address": signals["role_address"]["detected"],
        "tagged_address": signals["tagged_address"]["detected"],
        "mailbox_full": signals["mailbox_full"]["detected"],
        "do_not_reply": signals["do_not_reply"]["detected"],
        "irregular_pattern": signals["irregular_pattern"]["detected"],
        "unicode_or_suspicious_characters": signals["unicode_or_suspicious_characters"]["detected"],
        "secure_email_gateway": signals["secure_email_gateway"]["detected"],
    })
    return result


def prefetch_disposable_provider(email: object) -> None:
    """Schedule a domain lookup only when the local list has no verdict."""
    local, domain = _split_address(email)
    del local
    if not domain or domain in DISPOSABLE_DOMAINS:
        return
    from app.core.disposable_lookup import prefetch_disposable_domain

    prefetch_disposable_domain(domain)


def enrich_disposable_provider(
    result: dict[str, Any], *, allow_network: bool = True
) -> dict[str, Any]:
    """Supplement an unknown local-list result without affecting delivery checks."""
    ensure_email_risk_signals(result)
    signal = result["risk_signals"]["disposable_provider"]
    if signal.get("detected") is True:
        return result
    _local, domain = _split_address(result.get("email"))
    if not domain:
        return result
    from app.core.disposable_lookup import lookup_disposable_domain

    verdict = lookup_disposable_domain(domain, allow_network=allow_network)
    if verdict is None:
        return result
    signal.update({
        "detected": verdict,
        "detail": "public disposable-domain lookup" if verdict else "public disposable-domain lookup found no match",
        "domain": domain,
        "source": "debounce",
    })
    result["checks"]["disposable"] = verdict
    return result
