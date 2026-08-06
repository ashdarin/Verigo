"""Bounded related-domain discovery for company finder previews."""
from __future__ import annotations

import concurrent.futures
import json
import html as html_lib
import re
import socket
import ssl
import time
from urllib.parse import quote, urljoin, urlparse

from app.core.safe_http import safe_fetch

SUFFIXES = (".com", ".de", ".nl", ".fr", ".it", ".es", ".be", ".ch", ".at", ".co.uk", ".sg", ".ae", ".com.au", ".co.za", ".in", ".cn", ".jp", ".kr", ".hk", ".tw")
VERIFIED_DOMAIN_VARIANTS = {
    "porsche": ("porsche.com", "porsche.de"),
    "dieseltechnic": ("dieseltechnic.com", "dieseltechnic.de", "dieseltechnic.fr", "dieseltechnic.it", "dieseltechnic.es", "dieseltechnic.co.uk", "dieseltechnic.sg", "dieseltechnic.ae"),
    "bosch": ("bosch.com", "bosch.de", "bosch.nl", "bosch.fr", "bosch.it", "bosch.be", "bosch.ch", "bosch.at", "bosch.co.uk", "bosch.com.au"),
}
LOCATION_PATHS = ("/en/corporate-group/locations/", "/corporate-group/locations/", "/locations/", "/about/locations/", "/group/", "/worldwide/", "/contact/")
LEGAL_PATHS = ("/impressum/", "/imprint/", "/legal/", "/legal-notice/", "/legal-information/", "/mentions-legales/", "/note-legali/", "/en/impressum/", "/de/impressum/", "/fr/mentions-legales/", "/it/note-legali/", "/corporate-information/", "/about/", "/about-us/", "/company/", "/en/about/", "/en/company/", "/contact/", "/en/contact/")
LEGAL_LINK_HINTS = ("legal", "imprint", "impressum", "corporate", "company-information", "terms", "about")
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
    "microsoft": {
        "com": "Microsoft Corporation",
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


LEGAL_SUFFIX_RE = re.compile(r"\b(?:SE|SARL|S\.L\.|S\.R\.L\.|B\.V\.|Ltd|LTD\.?|Limited|GmbH|Pte\.?\s+Ltd|FZE|AG|SAS|S\.A\.|S\.p\.A\.|Corporation|Incorporated|Inc\.?|N\.V\.)\b", re.I)


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
            name = value.get("name")
            if isinstance(name, str) and LEGAL_SUFFIX_RE.search(name):
                return re.sub(r"\s+", " ", name).strip()[:180]
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
    legal_pattern = r"([A-Z][A-Za-z0-9().&,'’\-/]+(?:\s+[A-Z][A-Za-z0-9().&,'’\-/]+){0,8}\s+(?:SE|SARL|S\.L\.|S\.R\.L\.|B\.V\.|Ltd|LTD\.?|Limited|GmbH|Pte\.?\s+Ltd|FZE|AG|SAS|S\.p\.A\.|S\.A\.|SASU|Corporation|Incorporated|Inc\.?|N\.V\.))"
    for match in re.finditer(legal_pattern, text):
        candidate = re.sub(r"\s+", " ", match.group(1)).strip(" ,.;:")
        if len(candidate) >= 5:
            return candidate[:180]
    footer_pattern = r"(?:©|copyright|website of|internet pages of|all rights reserved)[^.;]{0,180}?([A-Z][A-Za-z0-9().&,'’\-/]+(?:\s+[A-Z][A-Za-z0-9().&,'’\-/]+){0,8}\s+(?:GmbH|B\.V\.|Ltd|AG|SAS|S\.p\.A\.|S\.A\.|Corporation|Inc\.?|Incorporated|N\.V\.))"
    match = re.search(footer_pattern, text, re.I)
    return re.sub(r"\s+", " ", match.group(1)).strip(" ,.;:")[:180] if match else None


def get_ssl_organization(domain: str) -> str | None:
    """Read the certificate organization when an OV/EV certificate exposes one."""
    try:
        from app.core.safe_http import has_only_public_addresses
        if not has_only_public_addresses(domain):
            return None
        context = ssl.create_default_context()
        with socket.create_connection((domain, 443), timeout=2.5) as sock:
            with context.wrap_socket(sock, server_hostname=domain) as secure_socket:
                certificate = secure_socket.getpeercert()
        subject = dict(item[0] for item in certificate.get("subject", []))
        organization = str(subject.get("organizationName") or "").strip()
        if organization and organization.lower() not in {"cloudflare, inc.", "let's encrypt"} and len(organization) > 3:
            return organization[:180]
    except Exception:
        pass
    return None


def resolve_official_entity(domain: str) -> dict[str, str] | None:
    """Find a legal entity from the company's own legal/corporate pages."""
    hosts = (domain, f"www.{domain}")
    candidate_urls: list[tuple[str, str]] = [(host, f"https://{host}{path}") for host in hosts for path in LEGAL_PATHS]
    deadline = time.monotonic() + 10.0
    active_host = domain
    for host in hosts:
        try:
            homepage_url = f"https://{host}"
            response = safe_fetch(homepage_url, timeout=2.5, max_bytes=220_000,
                                  allowed_hosts={domain, f"www.{domain}"})
            if response is None:
                continue
            source = response.body.decode("utf-8", "ignore")
            homepage_url = response.url or homepage_url
            active_host = urlparse(homepage_url).netloc or host
            for href, label in re.findall(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>([\s\S]{0,180}?)</a>", source, re.I):
                marker = f"{href} {re.sub(r'<[^>]+>', ' ', label)}".lower()
                parsed = urlparse(urljoin(homepage_url, href))
                if parsed.netloc and parsed.netloc.lower() == urlparse(homepage_url).netloc.lower() and any(hint in marker for hint in LEGAL_LINK_HINTS):
                    candidate_urls.append((host, urljoin(homepage_url, href)))
            homepage_name = _jsonld_legal_name(source) or _text_legal_name(source)
            if homepage_name:
                return {"legal_name": homepage_name, "source_url": homepage_url, "confidence": "medium"}
            # The www host is normally just a redirect alias. Avoid crawling it twice
            # when the canonical host already responded successfully.
            break
        except Exception:
            continue
    # Keep custom legal links discovered from the homepage and add static
    # fallbacks after them. The previous reassignment silently discarded the
    # most authoritative URL on many sites.
    discovered_urls = candidate_urls
    candidate_urls = discovered_urls + [(active_host, f"https://{active_host}{path}") for path in LEGAL_PATHS]
    seen: set[str] = set()
    prioritized_urls = []
    for _, url in candidate_urls:
        if url not in seen:
            seen.add(url)
            prioritized_urls.append(url)

    def fetch_candidate(url: str) -> tuple[str, str, str] | None:
        try:
            if time.monotonic() >= deadline:
                return None
            path = urlparse(url).path.rstrip("/")
            timeout = max(0.8, min(2.5, deadline - time.monotonic()))
            response = safe_fetch(url, timeout=timeout, max_bytes=220_000,
                                  allowed_hosts={domain, f"www.{domain}"})
            if response is None:
                return None
            source = response.body.decode("utf-8", "ignore")
            name = _jsonld_legal_name(source) or _text_legal_name(source)
            if name:
                confidence = "high" if any(marker in path.lower() for marker in ("corporate", "legal", "imprint", "impressum")) else "medium"
                return name, url, confidence
        except Exception:
            return None
        return None

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)
    futures = [pool.submit(fetch_candidate, url) for url in prioritized_urls]
    try:
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result:
                name, source_url, confidence = result
                pool.shutdown(wait=False, cancel_futures=True)
                return {"legal_name": name, "source_url": source_url, "confidence": confidence}
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    ssl_organization = get_ssl_organization(domain)
    # Public knowledge-graph fallback for sites that block automated legal pages.
    # The result is treated as medium confidence and retained with its source URL.
    sites = " ".join(f"<https://{host}{suffix}>" for host in (domain, f"www.{domain}") for suffix in ("", "/"))
    query = f"SELECT ?officialName WHERE {{ VALUES ?site {{ {sites} }} ?item wdt:P856 ?site . ?item wdt:P1448 ?officialName . }} LIMIT 1"
    try:
        endpoint = "https://query.wikidata.org/sparql?format=json&query=" + quote(query)
        response = safe_fetch(endpoint, timeout=4, max_bytes=80_000,
                              allowed_hosts={"query.wikidata.org"},
                              headers={"Accept": "application/sparql-results+json"})
        if response is None:
            raise OSError("Wikidata request was blocked")
        payload = json.loads(response.body.decode("utf-8", "ignore"))
        bindings = payload.get("results", {}).get("bindings", [])
        if bindings:
            name = bindings[0].get("officialName", {}).get("value")
            if isinstance(name, str) and name.strip():
                return {"legal_name": name.strip()[:180], "source_url": "https://www.wikidata.org/", "confidence": "medium"}
    except Exception:
        pass
    if ssl_organization:
        return {"legal_name": ssl_organization, "source_url": f"ssl://{domain}", "confidence": "low"}
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
    # A legal entity without jurisdiction evidence must not be copied to an
    # unrelated country domain. Leave the UI explicitly unconfirmed instead.
    return fallback

def discover_related(domain: str, title: str | None = None) -> tuple[list[dict[str, object]], list[str]]:
    sld = domain.split(".")[0] if domain.endswith((".co.uk", ".com.au", ".co.za")) else domain.rsplit(".", 1)[0]
    candidates = [candidate for candidate in VERIFIED_DOMAIN_VARIANTS.get(sld, ()) if candidate != domain]
    catalogued = sld in VERIFIED_DOMAIN_VARIANTS
    def probe(candidate: str):
        try:
            response = safe_fetch(f"https://{candidate}", timeout=2, max_bytes=8_000,
                                  allowed_hosts={candidate, f"www.{candidate}"})
            if response is None or response.status not in {200, 301, 302, 303, 307, 308}:
                return None
            suffix = candidate.rsplit(".", 1)[-1].lower()
            if candidate.endswith(".co.uk"):
                suffix = "uk"
            return {
                "domain": candidate,
                "url": f"https://{candidate}",
                "country": suffix.upper(),
                "title": None,
                "verified": True,
                "identity_confidence": "unconfirmed",
            }
        except Exception: return None
    # A catalog entry is only a candidate. Probe it just like any other
    # evidence-backed domain; unknown SLDs are never expanded into guessed
    # country suffixes.
    if catalogued:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            related = [item for item in pool.map(probe, candidates) if item][:16]
    else:
        related = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        entity_results = ([None] * len(related) if sld in LEGAL_ENTITY_OVERRIDES else
                          list(pool.map(lambda item: resolve_official_entity(str(item["domain"])), related)))
    for item, resolved in zip(related, entity_results):
        if resolved:
            item.update(resolved, title=resolved["legal_name"], identity_confidence=resolved.get("confidence", "medium"))
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
                response = safe_fetch(f"https://{domain}{path}", timeout=2, max_bytes=120_000,
                                      allowed_hosts={domain, f"www.{domain}"})
                if response is None:
                    continue
                html = response.body.decode("utf-8", "ignore")
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
            resolved_name = item.get("legal_name") or entity_for_country(sld, suffix, entities, "", index)
            if resolved_name:
                item["title"] = resolved_name
                if not item.get("legal_name") and LEGAL_SUFFIX_RE.search(resolved_name):
                    item["legal_name"] = resolved_name
                    item["identity_confidence"] = "medium"
                if sld in LEGAL_ENTITY_OVERRIDES and suffix in LEGAL_ENTITY_OVERRIDES[sld]:
                    item["legal_name"] = resolved_name
                    item["identity_confidence"] = "high"
    return related, entities[:8]
