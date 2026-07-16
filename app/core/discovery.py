from __future__ import annotations

import re


NAME_PART = re.compile(r"[^a-z0-9]+")
DOMAIN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")


def _name_part(value: str) -> str:
    normalized = NAME_PART.sub("", value.strip().lower())
    if not normalized:
        raise ValueError("请使用英文或拼音姓名")
    return normalized


def candidate_emails(first_name: str, last_name: str, domain: str) -> list[str]:
    first = _name_part(first_name)
    last = _name_part(last_name)
    domain = domain.strip().lower().removeprefix("@").removeprefix("http://").removeprefix("https://").strip("/")
    if not DOMAIN.fullmatch(domain):
        raise ValueError("请输入有效的公司域名，例如 company.com")
    f, l = first[0], last[0]
    locals_ = [
        f"{first}.{last}", f"{first}{last}", f"{first}_{last}", f"{first}-{last}",
        f"{last}.{first}", f"{last}{first}", f"{last}_{first}", f"{last}-{first}",
        f"{f}.{last}", f"{f}{last}", f"{f}_{last}", f"{f}-{last}",
        f"{first}.{l}", f"{first}{l}", f"{first}_{l}", f"{first}-{l}",
        f"{l}.{first}", f"{l}{first}", f"{l}_{first}", f"{l}-{first}",
        first, last, f"{first}.{last[0]}", f"{last}.{first[0]}",
    ]
    return list(dict.fromkeys(f"{local}@{domain}" for local in locals_))
