"""Read the derived name dictionary without storing a name-pair cross product."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from itertools import zip_longest

from app.config import settings


# The supplied World Name dataset has no AU file and "OTHER" is an
# application-level option. Use the closest broad Latin-name catalogues rather
# than falling back to a few hard-coded entries.
CATALOGUE_COUNTRY_FALLBACKS = {"AU": "GB", "OTHER": "US"}


def _names(country: str, kind: str, name_characters: int | None = None) -> tuple[str, ...]:
    """Return the most common usable spellings for a country and name kind."""
    path = settings.name_catalog_path
    if not path.is_file():
        return ()
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            if name_characters is None:
                rows = connection.execute(
                    """
                    SELECT romanized FROM name_entries
                    WHERE country=? AND kind=?
                    ORDER BY weight DESC, romanized
                    LIMIT ?
                    """,
                    (CATALOGUE_COUNTRY_FALLBACKS.get(country, country), kind, settings.name_catalog_pool_size),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT romanized FROM name_entries
                    WHERE country=? AND kind=? AND name_characters=?
                    ORDER BY weight DESC, romanized
                    LIMIT ?
                    """,
                    (country, kind, name_characters, settings.name_catalog_pool_size),
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
    """Yield each pair once, prioritizing the most common given and surname."""
    del domain, country
    return common_first_pairs(given_names, surnames)


def common_first_pairs(given_names: tuple[str, ...], surnames: tuple[str, ...]) -> Iterator[tuple[str, str]]:
    """Enumerate a Cartesian product in frequency-priority diagonals."""
    for diagonal in range(len(given_names) + len(surnames) - 1):
        start = max(0, diagonal - len(surnames) + 1)
        end = min(len(given_names) - 1, diagonal)
        for given_index in range(start, end + 1):
            surname_index = diagonal - given_index
            yield given_names[given_index], surnames[surname_index]


def chinese_pairs() -> Iterator[tuple[str, str]] | None:
    """Alternate two- and three-character Chinese full names, most common first."""
    one_character_given = _names("CN", "given", 1)
    two_character_given = _names("CN", "given", 2)
    surnames = _names("CN", "surname")
    if not one_character_given or not two_character_given or not surnames:
        return None
    # A Chinese full name is surname + given: one-character given names are
    # two-character full names, and two-character given names are three.
    return (
        pair
        for two_character, three_character in zip_longest(
            common_first_pairs(one_character_given, surnames),
            common_first_pairs(two_character_given, surnames),
        )
        for pair in (two_character, three_character)
        if pair is not None
    )


def country_pairs(
    domain: str,
    country: str,
    fallback_given: tuple[str, ...],
    fallback_surnames: tuple[str, ...],
) -> tuple[Iterator[tuple[str, str]], str]:
    """Use dictionary entries where available and fill a missing side safely."""
    if country == "CN":
        pairs = chinese_pairs()
        if pairs is not None:
            return pairs, "derived_name_catalogue"
    given_names = _names(country, "given") or fallback_given
    surnames = _names(country, "surname") or fallback_surnames
    source = "derived_name_catalogue" if (given_names != fallback_given or surnames != fallback_surnames) else "legacy_name_catalogue"
    return ranked_pairs(domain, country, given_names, surnames), source
