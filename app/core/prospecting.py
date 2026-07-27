"""Country-aware, bounded candidate generation for domain prospecting."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import replace
from dataclasses import dataclass
from itertools import zip_longest
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
    compound_given_names: tuple[str, ...] = ()
    compound_surnames: tuple[str, ...] = ()


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
    "alexander", "maximilian", "paul", "elias", "ben", "jonas", "leon", "felix", "luis", "noah",
    "lucas", "liam", "henry", "emil", "anton", "theo", "milan", "carl", "friedrich", "johann",
    "wilhelm", "heinrich", "klaus", "hans", "jurgen", "peter", "wolfgang", "michael", "thomas", "andreas",
    "stefan", "christian", "matthias", "daniel", "sebastian", "martin", "frank", "oliver", "tobias", "markus",
    "emma", "hanna", "marie", "anna", "sophia", "emilia", "lina", "klara", "ella", "mia",
    "lena", "lea", "amelie", "charlotte", "luisa", "johanna", "clara", "mathilda", "greta", "frieda",
    "petra", "sabine", "andrea", "gabriele", "monika", "ursula", "ingrid", "christa", "brigitte", "renate",
    "helga", "martina", "susanne", "angela", "claudia", "birgit", "katrin", "silke", "nicole", "karin",
)
GERMAN_SURNAMES = (
    "muller", "schmidt", "schneider", "fischer", "weber", "meyer", "wagner", "becker", "schulz", "hoffmann",
    "schaefer", "koch", "bauer", "richter", "klein", "wolf", "schroeder", "neumann", "schwarz", "zimmermann",
    "braun", "krueger", "hofmann", "hartmann", "lange", "schmitt", "werner", "schmitz", "krause", "meier",
    "lehmann", "schmid", "schulze", "maier", "koehler", "herrmann", "koenig", "walter", "mayer", "huber",
    "kaiser", "fuchs", "peters", "lang", "scholz", "moeller", "weiss", "jung", "hahn", "schubert",
    "vogel", "friedrich", "keller", "guenther", "frank", "berger", "winkler", "roth", "beck", "lorenz",
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
SPANISH_COMPOUND_SURNAMES = (
    "garcia lopez", "rodriguez martinez", "gonzalez fernandez", "lopez garcia", "martinez sanchez", "sanchez perez",
    "perez gomez", "gomez martin", "martin jimenez", "jimenez ruiz", "ruiz hernandez", "hernandez diaz",
    "diaz moreno", "moreno munoz", "munoz alvarez", "alvarez romero", "romero alonso", "alonso gutierrez",
    "gutierrez navarro", "navarro torres", "torres dominguez", "dominguez vazquez", "vazquez ramos", "ramos gil",
)
PORTUGUESE_SURNAMES = (
    "silva", "santos", "oliveira", "souza", "rodrigues", "ferreira", "alves", "pereira", "lima", "gomes",
    "costa", "ribeiro", "martins", "carvalho", "almeida", "lopes", "soares", "fernandes", "vieira", "barbosa",
    "rocha", "dias", "monteiro", "cardoso",
)
PORTUGUESE_COMPOUND_SURNAMES = (
    "silva santos", "santos oliveira", "oliveira souza", "souza rodrigues", "rodrigues ferreira", "ferreira alves",
    "alves pereira", "pereira lima", "lima gomes", "gomes costa", "costa ribeiro", "ribeiro martins",
    "martins carvalho", "carvalho almeida", "almeida lopes", "lopes soares", "soares fernandes", "fernandes vieira",
    "vieira barbosa", "barbosa rocha", "rocha dias", "dias monteiro", "monteiro cardoso", "cardoso correia",
)
NETHERLANDS_COMPOUND_SURNAMES = (
    "van der berg", "van den berg", "van dijk", "van der meer", "de jong", "de vries", "van der heijden",
    "van der laan", "van der wal", "van der pol", "van den bosch", "van der velde", "van der ploeg", "van der steen",
    "van der graaf", "van der veen", "van der hoef", "van der linden", "van der beek", "de groot", "de witte",
    "van leeuwen", "van der zand", "van der heide",
)
CHINESE_GIVEN = (
    "wei", "ming", "jun", "lei", "qiang", "jian", "tao", "yang", "bin", "bo", "hao",
    "lin", "jing", "yan", "fang", "li", "na", "mei", "xiao", "yu", "hui", "ying",
    "chen", "fei", "ning", "xuan", "rui", "yue", "chao", "peng", "hua", "wen", "gang",
    "feng", "long", "jie", "yong", "zhi", "guo", "qin", "xin", "yi", "jia", "han",
    "tian", "zhen", "lan", "ping", "dong", "hong", "ling", "juan", "qiao", "shan", "song",
    "qiu", "chun", "xia",
)
CHINESE_COMPOUND_GIVEN = (
    "zihao", "yuchen", "haoran", "zixuan", "yifan", "zihan", "yutong", "junhao", "mingyuan", "haoyu",
    "yuxuan", "chenxi", "zeyu", "tianyu", "jiahui", "xinyi", "yuting", "shihan", "wenjing", "xiaoyu",
    "jingyi", "mengyao", "ruoxi", "yuexin", "yihan", "jiayi", "wenhao", "zhiyuan", "haoxuan", "yuxin",
    "zeyuan", "xinyu", "yichen", "ziyang", "junjie", "wenbo", "haotian", "yiming", "yuxi", "xinyan",
    "jingwen", "jianning", "wenyu", "haolin", "yuehua", "xinyue", "jinghao", "yutian", "ziran", "wenxin",
    "haojun", "yutao", "yueying", "jingru", "zixia", "haichao", "zhiyu", "xiulan",
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
    "ES": CountryProfile("ES", SPANISH_GIVEN, SPANISH_SURNAMES, compound_surnames=SPANISH_COMPOUND_SURNAMES),
    "BR": CountryProfile("BR", SPANISH_GIVEN, PORTUGUESE_SURNAMES, compound_surnames=PORTUGUESE_COMPOUND_SURNAMES),
    "MX": CountryProfile("MX", SPANISH_GIVEN, SPANISH_SURNAMES, compound_surnames=SPANISH_COMPOUND_SURNAMES),
    "CN": CountryProfile("CN", CHINESE_GIVEN, CHINESE_SURNAMES, compound_given_names=CHINESE_COMPOUND_GIVEN),
    "JP": CountryProfile("JP", JAPANESE_GIVEN, JAPANESE_SURNAMES),
    "KR": CountryProfile("KR", KOREAN_GIVEN, KOREAN_SURNAMES),
    "IN": CountryProfile("IN", INDIAN_GIVEN, INDIAN_SURNAMES),
    "NL": CountryProfile("NL", ENGLISH_GIVEN, ENGLISH_SURNAMES, compound_surnames=NETHERLANDS_COMPOUND_SURNAMES),
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
    name_groups = [(profile.given_names, profile.surnames)]
    if profile.compound_given_names:
        name_groups.append((profile.compound_given_names, profile.surnames))
    if profile.compound_surnames:
        name_groups.append((profile.given_names, profile.compound_surnames))

    ranked_groups = [
        sorted(
            [(first, last.replace(" ", "")) for first in given_names for last in surnames],
            key=lambda pair: hashlib.blake2s(
                f"{domain}:{country}:{pair[0]}:{pair[1]}".encode("ascii"), digest_size=8
            ).digest(),
        )
        for given_names, surnames in name_groups
    ]
    # Change which equally weighted name shape appears first per domain while
    # keeping each domain's catalogue deterministic across successive runs.
    if len(ranked_groups) > 1 and hashlib.blake2s(
        f"{domain}:{country}:name-shape".encode("ascii"), digest_size=1
    ).digest()[0] % 2:
        ranked_groups.reverse()
    return [pair for batch in zip_longest(*ranked_groups) for pair in batch if pair is not None]


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
    name_pairs = _ranked_name_pairs(normalized_domain, normalized_country)
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

    # Exhaust the highest-confidence naming rule before trying a fallback rule.
    # This keeps successive runs focused on a known company convention.
    for pattern in personal_patterns:
        for first, last in name_pairs:
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
