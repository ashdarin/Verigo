#!/usr/bin/env python3
"""Measure email-pattern coverage before spending verification capacity.

This is an experiment workbench, not a replacement SMTP verifier.  It accepts
known contacts as the source of truth, produces a reviewable candidate manifest,
and only submits to Verigo when explicitly asked to do so.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean, median
from typing import Iterable


DEFAULT_PATTERNS = (
    "first.last",
    "flast",
    "firstlast",
    "first_last",
    "f.last",
    "last.first",
    "first",
    "last",
)

PATTERN_ALIASES = {
    "firstname.lastname": "first.last",
    "firstnamelastname": "firstlast",
    "firstname_lastname": "first_last",
    "firstname-lastname": "first-last",
    "f.lastname": "f.last",
    "flastname": "flast",
    "lastname.firstname": "last.first",
    "lastnamefirstname": "lastfirst",
    "lastname_firstname": "last_first",
    "vorname.nachname": "first.last",
    "vornamenachname": "firstlast",
    "v.nachname": "f.last",
    "vorname_nachname": "first_last",
    "vnachname": "flast",
    "nachname.vorname": "last.first",
    "nome.cognome": "first.last",
    "nomecognome": "firstlast",
    "n.cognome": "f.last",
    "nome_cognome": "first_last",
    "ncognome": "flast",
    "cognome.nome": "last.first",
    "prenom.nom": "first.last",
    "prenomnom": "firstlast",
    "p.nom": "f.last",
    "prenom_nom": "first_last",
    "pnom": "flast",
    "nom.prenom": "last.first",
    "nombre.apellido": "first.last",
    "nombreapellido": "firstlast",
    "n.apellido": "f.last",
    "nombre_apellido": "first_last",
    "napellido": "flast",
    "apellido.nombre": "last.first",
}


@dataclass(frozen=True)
class Contact:
    domain: str
    first_name: str
    last_name: str
    expected_email: str = ""
    role: str = ""


@dataclass(frozen=True)
class Candidate:
    domain: str
    first_name: str
    last_name: str
    role: str
    email: str
    pattern: str
    rank: int
    profile_source: str
    expected_email: str


def normalize_domain(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip() if "://" in value else f"//{value.strip()}")
    domain = (parsed.hostname or "").rstrip(".").lower()
    if domain.startswith("www."):
        domain = domain[4:]
    if not domain or "." not in domain:
        raise ValueError(f"Invalid company domain: {value!r}")
    return domain.encode("idna").decode("ascii")


def normalize_name_part(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.strip().lower())
    ascii_value = "".join(char for char in normalized if not unicodedata.combining(char))
    result = re.sub(r"[^a-z0-9]", "", ascii_value)
    if not result:
        raise ValueError(
            "Names must be supplied in an email-ready Latin spelling; "
            "do not silently invent a transliteration."
        )
    return result


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if not email:
        return ""
    local, separator, domain = email.partition("@")
    if not separator or not local:
        raise ValueError(f"Invalid expected_email: {value!r}")
    return f"{local}@{normalize_domain(domain)}"


def _legacy_patterns(domain: str) -> tuple[list[str], str]:
    """Bridge the first version's domain catalogue without copying its data."""
    legacy_path = Path(__file__).with_name("name_database (1).py")
    if not legacy_path.exists():
        return [], "generic"
    spec = importlib.util.spec_from_file_location("verigo_legacy_name_database", legacy_path)
    if spec is None or spec.loader is None:
        return [], "generic"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    database = module.InternationalNameDatabase()
    patterns = database.get_enterprise_patterns(domain)
    return list(patterns), "legacy_domain_catalogue" if database.get_company_info(domain) else "country_inference"


def ranked_patterns(domain: str) -> tuple[list[str], str]:
    raw_patterns, source = _legacy_patterns(domain)
    canonical = [PATTERN_ALIASES.get(pattern.lower(), pattern.lower()) for pattern in raw_patterns]
    ordered = list(dict.fromkeys([*canonical, *DEFAULT_PATTERNS]))
    supported = [
        pattern
        for pattern in ordered
        if pattern in {
            "first.last", "firstlast", "first_last", "first-last", "f.last", "flast",
            "last.first", "lastfirst", "last_first", "first", "last",
        }
    ]
    return supported, source


def render_local_part(pattern: str, first: str, last: str) -> str:
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
        "first": first,
        "last": last,
    }
    return values[pattern]


def candidates_for_contact(contact: Contact, limit: int) -> list[Candidate]:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    domain = normalize_domain(contact.domain)
    first = normalize_name_part(contact.first_name)
    last = normalize_name_part(contact.last_name)
    expected_email = normalize_email(contact.expected_email)
    patterns, source = ranked_patterns(domain)
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for pattern in patterns:
        email = f"{render_local_part(pattern, first, last)}@{domain}"
        if email in seen:
            continue
        seen.add(email)
        candidates.append(Candidate(
            domain=domain,
            first_name=contact.first_name,
            last_name=contact.last_name,
            role=contact.role,
            email=email,
            pattern=pattern,
            rank=len(candidates) + 1,
            profile_source=source,
            expected_email=expected_email,
        ))
        if len(candidates) == limit:
            break
    return candidates


def read_contacts(path: Path) -> list[Contact]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"domain", "first_name", "last_name"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("Input CSV must include domain, first_name, and last_name columns")
        contacts = [
            Contact(
                domain=row["domain"],
                first_name=row["first_name"],
                last_name=row["last_name"],
                expected_email=row.get("expected_email", ""),
                role=row.get("role", ""),
            )
            for row in reader
            if any((value or "").strip() for value in row.values())
        ]
    if not contacts:
        raise ValueError("Input CSV has no contacts")
    return contacts


def build_manifest(contacts: Iterable[Contact], limit_per_contact: int, max_candidates: int) -> list[Candidate]:
    manifest: list[Candidate] = []
    for contact in contacts:
        generated = candidates_for_contact(contact, limit_per_contact)
        if len(manifest) + len(generated) > max_candidates:
            raise ValueError(
                f"Candidate budget of {max_candidates} would be exceeded; split the experiment."
            )
        manifest.extend(generated)
    return manifest


def coverage_report(contacts: Iterable[Contact], manifest: Iterable[Candidate]) -> dict[str, object]:
    contact_list = list(contacts)
    by_contact: dict[tuple[str, str, str], list[Candidate]] = {}
    for candidate in manifest:
        key = (candidate.domain, candidate.first_name, candidate.last_name)
        by_contact.setdefault(key, []).append(candidate)

    labeled = [contact for contact in contact_list if contact.expected_email.strip()]
    hits: list[int] = []
    for contact in labeled:
        domain = normalize_domain(contact.domain)
        expected = normalize_email(contact.expected_email)
        candidates = by_contact[(domain, contact.first_name, contact.last_name)]
        rank = next((item.rank for item in candidates if item.email == expected), None)
        if rank is not None:
            hits.append(rank)

    candidate_counts = [
        len(by_contact[(normalize_domain(contact.domain), contact.first_name, contact.last_name)])
        for contact in contact_list
    ]
    return {
        "contacts": len(contact_list),
        "labeled_contacts": len(labeled),
        "matched_expected_emails": len(hits),
        "coverage": round(len(hits) / len(labeled), 4) if labeled else None,
        "top_1_coverage": round(sum(rank == 1 for rank in hits) / len(labeled), 4) if labeled else None,
        "mean_rank_when_matched": round(mean(hits), 2) if hits else None,
        "mean_candidates_per_contact": round(mean(candidate_counts), 2),
        "median_candidates_per_contact": median(candidate_counts),
        "note": (
            "Coverage measures only whether an independently known email appears in the candidate list. "
            "It does not claim identity precision or SMTP verification accuracy."
        ),
    }


def write_manifest(path: Path, manifest: Iterable[Candidate]) -> None:
    rows = [asdict(candidate) for candidate in manifest]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(Candidate.__dataclass_fields__))
        writer.writeheader()
        writer.writerows(rows)


def submit_to_verigo(
    manifest: list[Candidate],
    endpoint: str,
    api_key: str,
    worker_count: int,
    batch_size: int,
) -> list[dict[str, object]]:
    if not api_key:
        raise ValueError("An API key is required for live submission")
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    base_url = endpoint.rstrip("/")
    job_responses: list[dict[str, object]] = []
    emails = [candidate.email for candidate in manifest]
    for offset in range(0, len(emails), batch_size):
        payload = json.dumps({
            "emails": emails[offset: offset + batch_size],
            "worker_count": worker_count,
            "stop_on_deliverable": False,
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{base_url}/api/jobs",
            data=payload,
            headers={"Content-Type": "application/json", "X-API-Key": api_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                job_responses.append(json.load(response))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Verigo submission failed with HTTP {exc.code}: {detail}") from exc
    return job_responses


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="CSV: domain,first_name,last_name[,expected_email,role]")
    parser.add_argument("--manifest", type=Path, default=Path("prospecting_manifest.csv"))
    parser.add_argument("--report", type=Path, default=Path("prospecting_report.json"))
    parser.add_argument("--limit-per-contact", type=int, default=8)
    parser.add_argument("--max-candidates", type=int, default=500)
    parser.add_argument("--submit", action="store_true", help="Create verification jobs after writing local output")
    parser.add_argument("--allow-live-submission", action="store_true")
    parser.add_argument("--endpoint", default="https://verigo.site")
    parser.add_argument("--api-key-env", default="VERIGO_API_KEY")
    parser.add_argument("--worker-count", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=250)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    contacts = read_contacts(args.input)
    manifest = build_manifest(contacts, args.limit_per_contact, args.max_candidates)
    report = coverage_report(contacts, manifest)
    write_manifest(args.manifest, manifest)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Manifest: {args.manifest} ({len(manifest)} candidates)")

    if args.submit:
        if not args.allow_live_submission:
            raise ValueError("Live submission requires --allow-live-submission")
        api_key = os.environ.get(args.api_key_env, "")
        jobs = submit_to_verigo(manifest, args.endpoint, api_key, args.worker_count, args.batch_size)
        print(json.dumps({"submitted_jobs": jobs}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
