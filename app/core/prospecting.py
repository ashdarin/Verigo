"""Bounded, auditable candidate generation for the private prospecting beta."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlsplit


DOMAIN = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)

PUBLIC_MAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "hotmail.com", "outlook.com", "live.com",
    "yahoo.com", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com",
    "qq.com", "163.com", "126.com", "yandex.com", "yandex.ru",
})

ROLE_LOCAL_PARTS = (
    "sales", "info", "contact", "hello", "enquiries", "inquiries", "marketing",
    "business", "partnerships", "purchasing", "procurement", "press", "media",
    "support", "customerservice", "customersupport", "orders", "export", "office",
    "admin", "reception", "careers", "hr", "privacy",
)

# This is deliberately small. The beta measures controlled yield rather than
# creating an unbounded Cartesian product from a name catalogue.
GIVEN_NAMES = (
    "james", "john", "michael", "david", "robert", "daniel", "matthew", "andrew",
    "alexander", "christopher", "thomas", "richard", "william", "benjamin", "samuel",
    "emma", "olivia", "sophia", "isabella", "charlotte", "amelia", "jennifer",
    "elizabeth", "sarah", "julia", "laura", "anna", "martina", "nicole", "claudia",
)
SURNAMES = (
    "smith", "johnson", "williams", "brown", "jones", "miller", "davis", "wilson",
    "anderson", "thomas", "martin", "taylor", "lee", "moore", "white", "harris",
    "muller", "schmidt", "schneider", "fischer", "weber", "meyer", "wagner", "becker",
    "hoffmann", "klein", "bauer", "richter", "krause", "schulz",
)
DEFAULT_PERSON_PATTERNS = ("first.last", "f.last", "firstlast")
SUPPORTED_PERSON_PATTERNS = frozenset({
    "first.last", "firstlast", "first_last", "first-last", "f.last", "flast",
    "last.first", "lastfirst", "last_first",
})


@dataclass(frozen=True)
class ProspectingCandidate:
    email: str
    category: str
    pattern: str
    rank: int
    source: str


def normalize_company_domain(value: str) -> str:
    raw = value.strip().lower()
    parsed = urlsplit(raw if "://" in raw else f"//{raw}")
    domain = (parsed.hostname or "").rstrip(".")
    if domain.startswith("www."):
        domain = domain[4:]
    if not DOMAIN.fullmatch(domain):
        raise ValueError("请输入有效的公司域名，例如 company.com")
    if domain in PUBLIC_MAIL_DOMAINS:
        raise ValueError("请输入企业域名，不能使用公共邮箱服务域名")
    return domain


def _render_personal(pattern: str, first: str, last: str) -> str:
    values = {
        "first.last": f"{first}.{last}",
        "firstlast": f"{first}{last}",
        "first_last": f"{first}_{last}",
        "first-last": f"{first}-{last}",
        "f.last": f"{first[0]}.{last}",
        "flast": f"{first[0]}{last}",
        "last.first": f"{last}.{first}",
        "lastfirst": f"{last}{first}",
        "last_first": f"{last}_{first}",
    }
    return values[pattern]


def _ranked_name_pairs(domain: str) -> list[tuple[str, str]]:
    pairs = [(first, last) for first in GIVEN_NAMES for last in SURNAMES]
    return sorted(
        pairs,
        key=lambda pair: hashlib.blake2s(
            f"{domain}:{pair[0]}:{pair[1]}".encode("ascii"), digest_size=8
        ).digest(),
    )


def generate_candidates(
    domain: str,
    max_candidates: int,
    learned_patterns: Iterable[str] = (),
) -> list[ProspectingCandidate]:
    """Create a bounded candidate list with role accounts before name guesses."""
    normalized_domain = normalize_company_domain(domain)
    if max_candidates < len(ROLE_LOCAL_PARTS):
        raise ValueError("候选预算必须至少覆盖基础业务邮箱")
    learned = [pattern for pattern in learned_patterns if pattern in SUPPORTED_PERSON_PATTERNS]
    personal_patterns = list(dict.fromkeys([*learned, *DEFAULT_PERSON_PATTERNS]))
    candidates: list[ProspectingCandidate] = []
    seen: set[str] = set()

    def append(local: str, category: str, pattern: str, source: str) -> None:
        email = f"{local}@{normalized_domain}"
        if email in seen or len(candidates) >= max_candidates:
            return
        seen.add(email)
        candidates.append(ProspectingCandidate(
            email=email,
            category=category,
            pattern=pattern,
            rank=len(candidates) + 1,
            source=source,
        ))

    for local in ROLE_LOCAL_PARTS:
        append(local, "business_entry", f"role:{local}", "role_catalogue")

    source = "learned_domain_profile" if learned else "bounded_name_catalogue"
    for first, last in _ranked_name_pairs(normalized_domain):
        for pattern in personal_patterns:
            append(_render_personal(pattern, first, last), "personal_candidate", pattern, source)
            if len(candidates) >= max_candidates:
                return candidates
    return candidates
