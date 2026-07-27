from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.verification_outcome import (
    RETRY_DELAYED,
    RETRY_GREYLIST,
    RETRY_NEVER,
    apply_outcome,
    ensure_outcome,
    retry_policy,
)


def classified(result: dict[str, object]) -> dict[str, object]:
    ensure_outcome(result)
    return result


domain_missing = classified({
    "checks": {"format": True, "domain": False, "mx": False, "smtp": False},
    "smtp_result": "域名不存在，未发起SMTP验证",
})
assert retry_policy(domain_missing) == RETRY_NEVER
assert domain_missing["failure_stage"] == "dns"
assert domain_missing["failure_reason"] == "domain_nxdomain"

mx_missing = classified({
    "checks": {"format": True, "domain": True, "mx": False, "smtp": False},
    "smtp_result": "未找到MX记录，未发起SMTP验证",
})
assert retry_policy(mx_missing) == RETRY_NEVER
assert mx_missing["failure_reason"] == "mx_missing"

dns_transient = apply_outcome(
    {"smtp_result": "DNS 查询暂时失败，未发起SMTP验证"},
    stage="dns",
    reason="dns_transient",
    retry_policy=RETRY_DELAYED,
)
assert retry_policy(dns_transient) == RETRY_DELAYED

smtp_timeout = classified({"smtp_result": "SMTP连接超时（RCPT TO阶段）"})
assert retry_policy(smtp_timeout) == RETRY_DELAYED
assert smtp_timeout["failure_reason"] == "smtp_timeout"

legacy_smtp_unknown = classified({"smtp_result": "SMTP暂时无法确认: 无有效响应"})
assert retry_policy(legacy_smtp_unknown) == RETRY_DELAYED

smtp_421 = classified({"smtp_result": "421 service not available"})
assert retry_policy(smtp_421) == RETRY_DELAYED
assert smtp_421["smtp_code"] == "421"

greylisted = classified({"smtp_result": "450 Sender address rejected: Greylisted"})
assert retry_policy(greylisted) == RETRY_GREYLIST

smtp_550 = classified({"smtp_result": "550 mailbox unavailable"})
assert retry_policy(smtp_550) == RETRY_NEVER
assert smtp_550["failure_reason"] == "smtp_permanent"

mailbox_full = classified({"smtp_result": "452 4.2.2 mailbox over quota"})
assert retry_policy(mailbox_full) == RETRY_NEVER
assert mailbox_full["failure_reason"] == "mailbox_full"

print("verification outcome smoke: ok")
