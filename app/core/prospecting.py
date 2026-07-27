"""Country-aware, bounded candidate generation for domain prospecting."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import replace
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

SUPPORTED_PERSON_PATTERNS = frozenset({
    "first.last", "firstlast", "first_last", "first-last", "f.last", "flast",
    "last.first", "lastfirst", "last_first",
})
DEFAULT_PERSON_PATTERNS = ("first.last", "f.last", "firstlast")
ALL_PERSON_PATTERNS = tuple(sorted(SUPPORTED_PERSON_PATTERNS))


@dataclass(frozen=True)
class CountryProfile:
    code: str
    given_names: tuple[str, ...]
    surnames: tuple[str, ...]


ENGLISH_GIVEN = (
    "james", "john", "michael", "david", "robert", "daniel", "matthew", "andrew",
    "alexander", "christopher", "thomas", "richard", "william", "benjamin", "samuel",
    "emma", "olivia", "sophia", "isabella", "charlotte", "amelia", "jennifer",
    "elizabeth", "sarah", "julia", "laura", "anna", "grace", "natalie", "victoria",
)
ENGLISH_SURNAMES = (
    "smith", "johnson", "williams", "brown", "jones", "miller", "davis", "wilson",
    "anderson", "thomas", "martin", "taylor", "lee", "moore", "white", "harris",
    "clark", "lewis", "walker", "hall", "young", "king", "wright", "green", "baker",
    "adams", "nelson", "carter", "mitchell", "roberts",
)
GERMAN_GIVEN = (
    "luca", "leon", "paul", "jonas", "felix", "max", "ben", "finn", "moritz", "tim",
    "anna", "emma", "mia", "hannah", "lea", "lena", "julia", "laura", "sophie", "marie",
    "johannes", "thomas", "christian", "andreas", "markus", "sabine", "claudia", "martina",
)
GERMAN_SURNAMES = (
    "muller", "schmidt", "schneider", "fischer", "weber", "meyer", "wagner", "becker",
    "hoffmann", "klein", "bauer", "richter", "krause", "schulz", "hartmann", "lange",
    "schmitt", "werner", "schmitz", "kraus", "meier", "walter", "koch", "hofmann",
)
FRENCH_GIVEN = (
    "jean", "pierre", "michel", "philippe", "nicolas", "thomas", "julien", "antoine",
    "laurent", "francois", "marie", "camille", "julie", "sophie", "claire", "chloe",
    "amelie", "laura", "manon", "pauline", "alexandre", "benoit", "guillaume", "marc",
)
FRENCH_SURNAMES = (
    "martin", "bernard", "thomas", "petit", "robert", "richard", "durand", "dubois",
    "moreau", "laurent", "simon", "michel", "lefebvre", "leroy", "roux", "david",
    "bertrand", "morel", "fournier", "girard", "bonnet", "dupont", "lambert", "fontaine",
)
ITALIAN_GIVEN = (
    "marco", "luca", "matteo", "andrea", "davide", "alessandro", "giuseppe", "francesco",
    "antonio", "roberto", "giulia", "sofia", "francesca", "alessia", "chiara", "martina",
    "sara", "elena", "valentina", "silvia", "lorenzo", "stefano", "simone", "paolo",
)
ITALIAN_SURNAMES = (
    "rossi", "russo", "ferrari", "esposito", "bianchi", "romano", "colombo", "ricci",
    "marino", "greco", "bruno", "gallo", "conti", "de luca", "mancini", "costa",
    "giordano", "rizzo", "lombardi", "moretti", "barbieri", "fontana", "santoro", "mariani",
)
SPANISH_GIVEN = (
    "jose", "antonio", "manuel", "francisco", "david", "juan", "javier", "carlos",
    "miguel", "daniel", "maria", "carmen", "ana", "laura", "marta", "elena", "lucia",
    "paula", "sofia", "isabel", "alberto", "pablo", "sergio", "diego",
)
SPANISH_SURNAMES = (
    "garcia", "rodriguez", "gonzalez", "fernandez", "lopez", "martinez", "sanchez", "perez",
    "gomez", "martin", "jimenez", "ruiz", "hernandez", "diaz", "moreno", "munoz", "alvarez",
    "romero", "alonso", "gutierrez", "navarro", "torres", "dominguez", "vazquez",
)
CHINESE_GIVEN = (
    "wei", "ming", "jun", "lei", "qiang", "jian", "tao", "yang", "bin", "bo", "hao",
    "lin", "jing", "yan", "fang", "li", "na", "mei", "xiao", "yu", "hui", "ying",
    "chen", "fei", "ning", "xuan", "rui", "yue",
)
CHINESE_SURNAMES = (
    "wang", "li", "zhang", "liu", "chen", "yang", "huang", "zhao", "wu", "zhou", "xu",
    "sun", "ma", "zhu", "hu", "guo", "he", "lin", "luo", "gao", "liang", "xie", "song",
    "tang", "han", "feng", "yu", "dong",
)
JAPANESE_GIVEN = (
    "hiroshi", "takashi", "kenji", "yuki", "ryo", "kazuya", "taro", "daiki", "shota", "haruto",
    "yui", "yuka", "aiko", "mika", "sakura", "hana", "mei", "rika", "naomi", "emi",
    "masato", "koji", "akiko", "keiko", "yoshiko", "satoshi", "ayumi", "kaori",
)
JAPANESE_SURNAMES = (
    "sato", "suzuki", "takahashi", "tanaka", "watanabe", "ito", "yamamoto", "nakamura",
    "kobayashi", "kato", "yoshida", "yamada", "sasaki", "yamaguchi", "matsumoto", "inoue",
    "shimizu", "hayashi", "saito", "morita", "ishikawa", "maeda", "okada", "fujita",
)
KOREAN_GIVEN = (
    "minjun", "seojun", "jihoon", "hyunwoo", "junseo", "donghyun", "taeyang", "seungho",
    "jiyoon", "seoyeon", "minji", "sujin", "yejin", "jiwoo", "eunji", "hyejin",
    "jaehyun", "sungmin", "yuna", "soyoung", "taemin", "hyeon", "jimin", "seungmin",
)
KOREAN_SURNAMES = (
    "kim", "lee", "park", "choi", "jung", "kang", "cho", "yoon", "jang", "shin", "gwon",
    "hwang", "ahn", "song", "ryu", "oh", "han", "seo", "kwon", "bae", "lim", "noh", "yang", "ko",
)
INDIAN_GIVEN = (
    "arjun", "rahul", "rohan", "vikram", "amit", "raj", "sanjay", "anil", "deepak", "kiran",
    "priya", "ananya", "neha", "pooja", "kavita", "sneha", "divya", "meera", "rani", "isha",
    "aditya", "nitesh", "manish", "vivek", "sonal", "swati", "ashish", "varun",
)
INDIAN_SURNAMES = (
    "sharma", "verma", "gupta", "singh", "kumar", "patel", "shah", "mehta", "agarwal", "reddy",
    "nair", "iyer", "joshi", "kapoor", "malhotra", "bhat", "rao", "das", "roy", "chopra",
    "mishra", "saxena", "jain", "pandey",
)

COUNTRY_PROFILES = {
    "US": CountryProfile("US", ENGLISH_GIVEN, ENGLISH_SURNAMES),
    "GB": CountryProfile("GB", ENGLISH_GIVEN, ENGLISH_SURNAMES),
    "CA": CountryProfile("CA", ENGLISH_GIVEN, ENGLISH_SURNAMES),
    "AU": CountryProfile("AU", ENGLISH_GIVEN, ENGLISH_SURNAMES),
    "DE": CountryProfile("DE", GERMAN_GIVEN, GERMAN_SURNAMES),
    "FR": CountryProfile("FR", FRENCH_GIVEN, FRENCH_SURNAMES),
    "IT": CountryProfile("IT", ITALIAN_GIVEN, ITALIAN_SURNAMES),
    "ES": CountryProfile("ES", SPANISH_GIVEN, SPANISH_SURNAMES),
    "BR": CountryProfile("BR", SPANISH_GIVEN, SPANISH_SURNAMES),
    "MX": CountryProfile("MX", SPANISH_GIVEN, SPANISH_SURNAMES),
    "CN": CountryProfile("CN", CHINESE_GIVEN, CHINESE_SURNAMES),
    "JP": CountryProfile("JP", JAPANESE_GIVEN, JAPANESE_SURNAMES),
    "KR": CountryProfile("KR", KOREAN_GIVEN, KOREAN_SURNAMES),
    "IN": CountryProfile("IN", INDIAN_GIVEN, INDIAN_SURNAMES),
    "NL": CountryProfile("NL", ENGLISH_GIVEN, ENGLISH_SURNAMES),
    "SE": CountryProfile("SE", ENGLISH_GIVEN, ENGLISH_SURNAMES),
    "CH": CountryProfile("CH", GERMAN_GIVEN, GERMAN_SURNAMES),
    "PL": CountryProfile("PL", ENGLISH_GIVEN, ENGLISH_SURNAMES),
    "TR": CountryProfile("TR", ENGLISH_GIVEN, ENGLISH_SURNAMES),
    "OTHER": CountryProfile("OTHER", ENGLISH_GIVEN, ENGLISH_SURNAMES),
}
SUPPORTED_COUNTRY_CODES = frozenset(COUNTRY_PROFILES)


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
        raise ValueError("Please enter a valid company domain, for example company.com")
    if domain in PUBLIC_MAIL_DOMAINS:
        raise ValueError("Please enter a company domain, not a public mailbox domain")
    return domain


def normalize_country(value: str) -> str:
    country = value.strip().upper()
    if country not in SUPPORTED_COUNTRY_CODES:
        raise ValueError("Please select a supported company country")
    return country


def normalize_person_pattern(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    pattern = value.strip().lower()
    if pattern not in SUPPORTED_PERSON_PATTERNS:
        raise ValueError("Please select a supported email naming rule")
    return pattern


def normalize_name_for_email(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.strip().lower())
    ascii_value = "".join(char for char in normalized if not unicodedata.combining(char))
    result = re.sub(r"[^a-z0-9]", "", ascii_value)
    if not result:
        raise ValueError("Known contact names must use an email-ready Latin spelling")
    return result


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


def infer_email_pattern(domain: str, first_name: str, last_name: str, email: str) -> str:
    """Infer a supported pattern from one known employee email address."""
    normalized_domain = normalize_company_domain(domain)
    first = normalize_name_for_email(first_name)
    last = normalize_name_for_email(last_name)
    sample = email.strip().lower()
    local, separator, email_domain = sample.partition("@")
    if not separator or email_domain != normalized_domain:
        raise ValueError("Known contact email must belong to the submitted company domain")
    for pattern in ALL_PERSON_PATTERNS:
        if _render_personal(pattern, first, last) == local:
            return pattern
    raise ValueError("The known contact does not match a supported email naming rule")


def _ranked_name_pairs(domain: str, country: str) -> list[tuple[str, str]]:
    profile = COUNTRY_PROFILES[country]
    pairs = [
        (first, last.replace(" ", ""))
        for first in profile.given_names
        for last in profile.surnames
    ]
    return sorted(
        pairs,
        key=lambda pair: hashlib.blake2s(
            f"{domain}:{country}:{pair[0]}:{pair[1]}".encode("ascii"), digest_size=8
        ).digest(),
    )


def generate_candidates(
    domain: str,
    country: str,
    max_candidates: int,
    learned_patterns: Iterable[str] = (),
    requested_pattern: str | None = None,
) -> list[ProspectingCandidate]:
    """Generate a fixed candidate budget with explicit evidence ordering."""
    normalized_domain = normalize_company_domain(domain)
    normalized_country = normalize_country(country)
    selected_pattern = normalize_person_pattern(requested_pattern)
    if max_candidates < len(ROLE_LOCAL_PARTS):
        raise ValueError("Candidate budget must cover the standard business mailboxes")

    learned = [
        pattern for pattern in learned_patterns
        if pattern in SUPPORTED_PERSON_PATTERNS and pattern != selected_pattern
    ]
    personal_patterns = list(dict.fromkeys([
        *([selected_pattern] if selected_pattern else []), *learned,
        *DEFAULT_PERSON_PATTERNS, *ALL_PERSON_PATTERNS,
    ]))
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

    for first, last in _ranked_name_pairs(normalized_domain, normalized_country):
        for pattern in personal_patterns:
            source = (
                "user_selected_pattern" if pattern == selected_pattern
                else "learned_domain_profile" if pattern in learned
                else f"country_name_catalogue:{normalized_country}"
            )
            append(_render_personal(pattern, first, last), "personal_candidate", pattern, source)
            if len(candidates) >= max_candidates:
                return candidates
    return candidates


def rerank_candidates(candidates: Iterable[ProspectingCandidate]) -> list[ProspectingCandidate]:
    """Return a contiguous rank sequence after previously emitted rows are removed."""
    return [replace(candidate, rank=index) for index, candidate in enumerate(candidates, start=1)]
