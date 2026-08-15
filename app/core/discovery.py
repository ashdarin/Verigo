from __future__ import annotations

import re


NAME_PART = re.compile(r"[^a-z0-9]+")
DOMAIN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")


# Ordered from the most common convention to less frequently observed, but
# still mainstream, address formats.  Keep this list at 25: discovery jobs
# are deliberately bounded so one lookup cannot turn into an open-ended
# guessing batch.
PERSONAL_LOCAL_FORMATS = (
    "{first}.{last}", "{first}{last}", "{first}_{last}", "{first}-{last}",
    "{last}.{first}", "{last}{first}", "{last}_{first}", "{last}-{first}",
    "{f}.{last}", "{f}{last}", "{f}_{last}", "{f}-{last}",
    "{first}.{l}", "{first}{l}", "{first}_{l}", "{first}-{l}",
    "{l}.{first}", "{l}{first}", "{l}_{first}", "{l}-{first}",
    "{last}.{f}", "{last}{f}", "{last}_{f}", "{last}-{f}",
    "{f}.{l}",
)


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
        pattern.format(first=first, last=last, f=f, l=l)
        for pattern in PERSONAL_LOCAL_FORMATS
    ]
    # Short single-character names can make two otherwise distinct patterns
    # render to the same local part. Preserve order while never verifying one
    # email address twice.
    return list(dict.fromkeys(f"{local}@{domain}" for local in locals_))
