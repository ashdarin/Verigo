from __future__ import annotations


QQ_CONSUMER_DOMAINS = frozenset({"qq.com", "vip.qq.com", "foxmail.com"})
YAHOO_UNSUPPORTED_MESSAGE = (
    "暂不支持 Yahoo 邮箱验证（含所有国家或地区后缀，以及 ymail.com、rocketmail.com）。"
    "Yahoo 的反验证策略非常严格，当前全网常规验证都难以稳定通过，暂时没有可靠解决方案。"
)


def email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].strip().lower() if "@" in email else ""


def is_qq_domain(domain: str) -> bool:
    return domain.strip().lower() in QQ_CONSUMER_DOMAINS


def is_qq_email(email: str) -> bool:
    return is_qq_domain(email_domain(email))


def is_yahoo_domain(domain: str) -> bool:
    normalized = domain.strip().lower().rstrip(".")
    return (
        normalized.startswith("yahoo.")
        or normalized in {"ymail.com", "rocketmail.com"}
    )


def is_yahoo_email(email: str) -> bool:
    return is_yahoo_domain(email_domain(email))


def yahoo_addresses(emails: list[str]) -> list[str]:
    return [email for email in emails if is_yahoo_email(email)]
