"""Bounded related-domain discovery for company finder previews."""
from __future__ import annotations

import concurrent.futures
import ipaddress
import re
import socket
from urllib.request import Request, urlopen

SUFFIXES = (".com", ".de", ".nl", ".fr", ".it", ".es", ".be", ".ch", ".at", ".co.uk", ".sg", ".ae", ".com.au", ".co.za", ".in", ".cn", ".jp", ".kr", ".hk", ".tw")
LOCATION_PATHS = ("/en/corporate-group/locations/", "/corporate-group/locations/", "/locations/", "/about/locations/", "/group/", "/worldwide/", "/contact/")
COUNTRY_NAMES = {
    "com": "Global", "de": "Germany", "nl": "Netherlands", "fr": "France", "it": "Italy",
    "es": "Spain", "be": "Belgium", "ch": "Switzerland", "at": "Austria", "uk": "United Kingdom",
    "sg": "Singapore", "ae": "United Arab Emirates", "au": "Australia", "za": "South Africa",
    "in": "India", "cn": "China", "jp": "Japan", "kr": "South Korea", "hk": "Hong Kong", "tw": "Taiwan",
}

def discover_related(domain: str, title: str | None = None) -> tuple[list[dict[str, object]], list[str]]:
    sld = domain.split(".")[0] if domain.endswith((".co.uk", ".com.au", ".co.za")) else domain.rsplit(".", 1)[0]
    candidates = [f"{sld}{suffix}" for suffix in SUFFIXES if f"{sld}{suffix}" != domain]
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
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        related = [item for item in pool.map(probe, candidates) if item][:8]
    entities = [title] if title else []
    for path in LOCATION_PATHS:
        try:
            with urlopen(Request(f"https://{domain}{path}", headers={"User-Agent": "VerigoDomainPreview/1.0"}), timeout=2) as response:
                html = response.read(120_000).decode("utf-8", "ignore")
            for item in re.findall(r"(?:[A-Z][A-Za-z]+\s+){1,4}(?:SE|SARL|S\.L\.|Ltd|Limited|GmbH|S\.R\.L\.)", html):
                item = re.sub(r"\s+", " ", item).strip()
                if item not in entities: entities.append(item)
            if len(entities) >= 8: break
        except Exception: continue
    return related, entities[:8]
