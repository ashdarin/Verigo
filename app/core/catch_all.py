from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any


def _domain(result: dict[str, Any]) -> str:
    email = str(result.get("email") or "")
    if "@" not in email:
        return ""
    return email.rsplit("@", 1)[1].lower()


def _has_explicit_550_rejection(result: dict[str, Any]) -> bool:
    """A recipient-level 550 is stronger evidence than a prior probe verdict."""
    if result.get("deliverable") is not False:
        return False
    detail = " ".join(
        str(result.get(key) or "")
        for key in ("smtp_code", "smtp_result", "smtp_raw_result", "message")
    )
    return re.search(r"(?<!\d)550(?!\d)", detail) is not None


def reconcile_catch_all_conflicts(results: Iterable[dict[str, Any]]) -> set[str]:
    """Downgrade Catch-all verdicts contradicted by SMTP recipient evidence."""
    items = list(results)
    accepted_domains = {
        _domain(result)
        for result in items
        if result.get("deliverable") is True
        and result.get("domain_type") != "catch-all"
        and _domain(result)
    }
    catch_all_domains = {
        _domain(result)
        for result in items
        if result.get("domain_type") == "catch-all" and _domain(result)
    }
    rejected_domains = {
        _domain(result)
        for result in items
        if _has_explicit_550_rejection(result) and _domain(result)
    }
    # A real 250 and a real 550 both disprove a domain-wide Catch-all claim.
    conflicts = (accepted_domains | rejected_domains) & catch_all_domains
    if not conflicts:
        return set()

    for result in items:
        if _domain(result) not in conflicts or result.get("domain_type") != "catch-all":
            continue
        result["domain_type"] = "inconclusive"
        result["valid"] = None
        result["deliverable"] = None
        result["verification_method"] = "catch-all_conflict"
        result["smtp_result"] = "邮件服务器返回了相互矛盾的信号，暂无法确认"
        result["message"] = result["smtp_result"]
        result["failure_stage"] = "smtp"
        result["failure_reason"] = "catch_all_conflict"
        result["retry_policy"] = "never"
        checks = result.get("checks")
        if isinstance(checks, dict):
            checks["smtp"] = None
    return conflicts
