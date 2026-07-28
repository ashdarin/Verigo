from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def _domain(result: dict[str, Any]) -> str:
    email = str(result.get("email") or "")
    if "@" not in email:
        return ""
    return email.rsplit("@", 1)[1].lower()


def reconcile_catch_all_conflicts(results: Iterable[dict[str, Any]]) -> set[str]:
    """Downgrade contradictory catch-all results to an inconclusive state."""
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
    conflicts = accepted_domains & catch_all_domains
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
