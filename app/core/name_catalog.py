"""Read the derived name dictionary without storing a name-pair cross product."""
from __future__ import annotations

import hashlib
import math
import sqlite3
from collections.abc import Iterator

from app.config import settings


# The supplied World Name dataset has no AU file and "OTHER" is an
# application-level option. Use the closest broad Latin-name catalogues rather
# than falling back to a few hard-coded entries.
CATALOGUE_COUNTRY_FALLBACKS = {"AU": "GB", "OTHER": "US"}


def _names(country: str, kind: str) -> tuple[str, ...]:
    """Return the most common usable spellings for a country and name kind."""
    path = settings.name_catalog_path
    if not path.is_file():
        return ()
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            rows = connection.execute(
                """
                SELECT romanized FROM name_entries
                WHERE country=? AND kind=?
                ORDER BY weight DESC, romanized
                LIMIT ?
                """,
                (CATALOGUE_COUNTRY_FALLBACKS.get(country, country), kind, settings.name_catalog_pool_size),
            ).fetchall()
    except sqlite3.Error:
        # A missing or mid-replacement derived catalogue must not stop discovery.
        return ()
    return tuple(str(row[0]) for row in rows)


def _hash_int(value: str) -> int:
    return int.from_bytes(hashlib.blake2s(value.encode("ascii"), digest_size=8).digest(), "big")


def ranked_pairs(
    domain: str,
    country: str,
    given_names: tuple[str, ...],
    surnames: tuple[str, ...],
) -> Iterator[tuple[str, str]]:
    """Yield every pair once in a deterministic domain-specific permutation."""
    total = len(given_names) * len(surnames)
    if not total:
        return
    seed = _hash_int(f"{domain}:{country}:derived-name-catalogue")
    start = seed % total
    stride = (seed // max(1, total)) % total or 1
    # A coprime step visits every position exactly once without sorting N*M pairs.
    while math.gcd(stride, total) != 1:
        stride = (stride + 1) % total or 1
    for offset in range(total):
        position = (start + offset * stride) % total
        yield given_names[position // len(surnames)], surnames[position % len(surnames)]


def country_pairs(
    domain: str,
    country: str,
    fallback_given: tuple[str, ...],
    fallback_surnames: tuple[str, ...],
) -> tuple[Iterator[tuple[str, str]], str]:
    """Use dictionary entries where available and fill a missing side safely."""
    given_names = _names(country, "given") or fallback_given
    surnames = _names(country, "surname") or fallback_surnames
    source = "derived_name_catalogue" if (given_names != fallback_given or surnames != fallback_surnames) else "legacy_name_catalogue"
    return ranked_pairs(domain, country, given_names, surnames), source
