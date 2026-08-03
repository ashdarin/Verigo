"""Bounded related-domain discovery for company finder previews."""
from __future__ import annotations

import concurrent.futures
import json
import html as html_lib
import ipaddress
import re
import socket
from urllib.request import Request, urlopen

SUFFIXES = (".com", ".de", ".nl", ".fr", ".it", ".es", ".be", ".ch", ".at", ".co.uk", ".sg", ".ae", ".com.au", ".co.za", ".in", ".cn", ".jp", ".kr", ".hk", ".tw")
VERIFIED_DOMAIN_VARIANTS = {
    "porsche": ("porsche.com", "porsche.de"),
    "dieseltechnic": ("dieseltechnic.com", "dieseltechnic.de", "dieseltechnic.fr", "dieseltechnic.it", "dieseltechnic.es", "dieseltechnic.co.uk", "dieseltechnic.sg", "dieseltechnic.ae"),
    "bosch": ("bosch.com", "bosch.de", "bosch.nl", "bosch.fr", "bosch.it", "bosch.be", "bosch.ch", "bosch.at", "bosch.co.uk", "bosch.com.au"),
}
LOCATION_PATHS = ("/en/corporate-group/locations/", "/corporate-group/locations/", "/locations/", "/about/locations/", "/group/", "/worldwide/", "/contact/")
LEGAL_PATHS = ("/corporate-information/", "/legal-notice/", "/imprint/", "/impressum/", "/contact/")
COUNTRY_NAMES = {
    "com": "Global", "de": "Germany", "nl": "Netherlands", "fr": "France", "it": "Italy",
    "es": "Spain", "be": "Belgium", "ch": "Switzerland", "at": "Austria", "uk": "United Kingdom",
    "sg": "Singapore", "ae": "United Arab Emirates", "au": "Australia", "za": "South Africa",
    "in": "India", "cn": "China", "jp": "Japan", "kr": "South Korea", "hk": "Hong Kong", "tw": "Taiwan",
}
LEGAL_ENTITY_OVERRIDES = {
    "dieseltechnic": {
        "com": "Diesel Technic SE",
        "de": "Diesel Technic SE",
        "nl": "Diesel Technic Benelux B.V.",
        "fr": "Diesel Technic France SARL",
        "es": "Diesel Technic Iberia S.L.",
        "it": "Diesel Technic Italia S.R.L.",
        "uk": "Diesel Technic UK & Ireland LTD.",
        "ae": "Diesel Technic (M.E.) FZE",
        "sg": "Diesel Technic Asia Pacific Pte Ltd",
    },
    "bosch": {
        "com": "Robert Bosch GmbH",
        "de": "Robert Bosch GmbH",
        "nl": "Robert Bosch B.V.",
        "fr": "Robert Bosch (France) SAS",
        "it": "Robert Bosch S.p.A. Societa' Unipersonale",
        "be": "NV. Robert Bosch S.A.",
        "ch": "Robert Bosch AG",
        "at": "Robert Bosch Aktiengesellschaft",
        "uk": "Robert Bosch UK Holdings Ltd",
        "au": "Robert Bosch (Australia) Pty Ltd",
    },
}
COUNTRY_ENTITY_HINTS = {
    "de": ("germany", "deutschland"), "nl": ("netherlands", "benelux", "nederland"),
    "fr": ("france", "français"), "it": ("italia", "italy"), "es": ("iberia", "spain", "españa"),
    "uk": ("united kingdom", "uk", "ireland"), "sg": ("singapore", "asia pacific"),
    "ae": ("united arab emirates", "m.e.", "middle east"), "au": ("australia",),
    "za": ("south africa",), "in": ("india",), "cn": ("china",), "jp": ("japan",),
    "kr": ("south korea", "korea"), "hk": ("hong kong",), "tw": ("taiwan",),
}


LEGAL_SUFFIX_RE = re.compile(r"\b(?:SE|SARL|S\.L\.|S\.R\.L\.|B\.V\.|Ltd|LTD\.?|Limited|GmbH|Pte\.?\s+Ltd|FZE|AG|SAS|S\.A\.)\b", re.I)


def _visible_text(source: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", source, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def _jsonld_legal_name(source: str) -> str | None:
    blocks = re.findall(r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>([\s\S]*?)</script>", source, re.I)
    def walk(value: object) -> str | None:
        if isinstance(value, dict):
            legal = value.get("legalName")
            if isinstance(legal, str) and legal.strip():
                return re.sub(r"\s+", " ", legal).strip()[:180]
            for child in value.values():
                found = walk(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = walk(child)
                if found:
                    return found
        return None
    for block in blocks:
        try:
            found = walk(json.loads(html_lib.unescape(block)))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if found:
            return found
    return None


def _text_legal_name(source: str) -> str | None:
    text = _visible_text(source)
    legal_pattern = r"([A-Z][A-Za-z0-9().&,'’\-/]+(?:\s+[A-Z][A-Za-z0-9().&,'’\-/]+){0,8}\s+(?:SE|SARL|S\.L\.|S\.R\.L\.|B\.V\.|Ltd|LTD\.?|Limited|GmbH|Pte\.?\s+Ltd|FZE|AG|SAS|S\.p\.A\.|S\.A\.|SASU))"
    for match in re.finditer(legal_pattern, text):
        candidate = re.sub(r"\s+", " ", match.group(1)).strip(" ,.;:")
        if len(candidate) >= 5:
            return candidate[:180]
    footer_pattern = r"(?:©|copyright|website of|internet pages of|all rights reserved)[^.;]{0,180}?([A-Z][A-Za-z0-9().&,'’\-/]+(?:\s+[A-Z][A-Za-z0-9().&,'’\-/]+){0,8}\s+(?:GmbH|B\.V\.|Ltd|AG|SAS|S\.p\.A\.|S\.A\.))"
    match = re.search(footer_pattern, text, re.I)
    return re.sub(r"\s+", " ", match.group(1)).strip(" ,.;:")[:180] if match else None


def resolve_official_entity(domain: str) -> dict[str, str] | None:
    """Find a legal entity from the company's own legal/corporate pages."""
    for path in LEGAL_PATHS:
        try:
            url = f"https://{domain}{path}"
            with urlopen(Request(url, headers={"User-Agent": "VerigoDomainPreview/1.0"}), timeout=2.5) as response:
                source = response.read(220_000).decode("utf-8", "ignore")
            name = _jsonld_legal_name(source)
            confidence = "high"
            if not name:
                name = _text_legal_name(source)
                confidence = "high" if path.rstrip("/") in {"/corporate-information", "/legal-notice", "/imprint", "/impressum"} else "medium"
            if name:
                return {"legal_name": name, "source_url": url, "confidence": confidence}
        except Exception:
            continue
    return None


def entity_for_country(sld: str, suffix: str, entities: list[str], fallback: str, index: int) -> str:
    override = LEGAL_ENTITY_OVERRIDES.get(sld, {}).get(suffix)
    if override:
        return override
    hints = COUNTRY_ENTITY_HINTS.get(suffix, ())
    for entity in entities:
        lowered = entity.lower()
        if any(hint in lowered for hint in hints):
            return entity
    legal_entities = [entity for entity in entities if LEGAL_SUFFIX_RE.search(entity)]
    if len(legal_entities) == 1:
        return legal_entities[0]
    if legal_entities and index < len(legal_entities):
        return legal_entities[index]
    return fallback

def discover_related(domain: str, title: str | None = None) -> tuple[list[dict[str, object]], list[str]]:
    sld = domain.split(".")[0] if domain.endswith((".co.uk", ".com.au", ".co.za")) else domain.rsplit(".", 1)[0]
    candidates = [candidate for candidate in VERIFIED_DOMAIN_VARIANTS.get(sld, ()) if candidate != domain]
    if not candidates:
        candidates = [f"{sld}{suffix}" for suffix in SUFFIXES[:12] if f"{sld}{suffix}" != domain]
    catalogued = sld in VERIFIED_DOMAIN_VARIANTS
    def probe(candidate: str):
        try:
            addresses = {ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(candidate, 443, type=socket.SOCK_STREAM)}
            if not addresses or any(not address.is_global for address in addresses): return None
            with urlopen(Request(f"https://{candidate}", headers={"User-Agent": "VerigoDomainPreview/1.0"}, method="HEAD"), timeout=2) as response:
                if response.status not in {200, 301, 302, 303, 307, 308}: return None
            suffix = candidate.rsplit(".", 1)[-1].lower()
            if candidate.endswith(".co.uk"):
                suffix = "uk"
            brand = re.sub(r"[-_]+", " ", sld).strip().title()
            return {
                "domain": candidate,
                "url": f"https://{candidate}",
                "country": suffix.upper(),
                "title": f"{brand} {COUNTRY_NAMES.get(suffix, suffix.upper())}".strip(),
            }
        except Exception: return None
    if catalogued:
        related = []
        for candidate in candidates[:16]:
            suffix = "uk" if candidate.endswith(".co.uk") else candidate.rsplit(".", 1)[-1].lower()
            brand = re.sub(r"[-_]+", " ", sld).strip().title()
            related.append({"domain": candidate, "url": f"https://{candidate}", "country": suffix.upper(), "title": f"{brand} {COUNTRY_NAMES.get(suffix, suffix.upper())}", "verified": True})
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            related = [item for item in pool.map(probe, candidates) if item][:16]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        entity_results = ([None] * len(related) if sld in LEGAL_ENTITY_OVERRIDES else
                          list(pool.map(lambda item: resolve_official_entity(str(item["domain"])), related)))
    for item, resolved in zip(related, entity_results):
        if resolved:
            item.update(resolved, title=resolved["legal_name"])
    entities = [title] if title else []
    main_suffix = "uk" if domain.endswith(".co.uk") else domain.rsplit(".", 1)[-1].lower()
    main_entity = None
    if sld in LEGAL_ENTITY_OVERRIDES and LEGAL_ENTITY_OVERRIDES[sld].get(main_suffix):
        main_entity = {"legal_name": LEGAL_ENTITY_OVERRIDES[sld][main_suffix], "confidence": "high"}
    else:
        main_entity = resolve_official_entity(domain)
    if main_entity:
        entities.insert(0, main_entity["legal_name"])
    if not main_entity:
        for path in LOCATION_PATHS:
            try:
                with urlopen(Request(f"https://{domain}{path}", headers={"User-Agent": "VerigoDomainPreview/1.0"}), timeout=2) as response:
                    html = response.read(120_000).decode("utf-8", "ignore")
                text = re.sub(r"<[^>]+>", " ", html)
                text = re.sub(r"\s+", " ", text)
                for item in re.findall(r"(?:[A-Z][A-Za-z().&-]+\s+){1,7}(?:SE|SARL|S\.L\.|S\.R\.L\.|B\.V\.|Ltd|LTD\.?|Limited|GmbH|Pte\.?\s+Ltd|FZE)", text):
                    item = re.sub(r"\s+", " ", item).strip()
                    if item not in entities: entities.append(item)
                if len(entities) >= 8: break
            except Exception: continue
    for index, item in enumerate(related):
        suffix = str(item.get("country", "")).lower()
        if suffix:
            resolved_name = entity_for_country(sld, suffix, entities, str(item.get("title") or ""), index)
            item["title"] = resolved_name
            if sld in LEGAL_ENTITY_OVERRIDES and suffix in LEGAL_ENTITY_OVERRIDES[sld]:
                item["legal_name"] = resolved_name
    return related, entities[:8]
