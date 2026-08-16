"""Backfill country metadata for the standalone Company Finder vitality index."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import duckdb

from company_vitality import (
    LEGAL_EVIDENCE_MARKETS,
    VitalityStore,
    iso_at,
    normalize_market,
    refresh_priority,
)


DEFAULT_CATALOGUE = "/opt/verigo-company-finder/data/company_catalog.duckdb"
DEFAULT_VITALITY = "/opt/verigo-company-finder/data/company_vitality.sqlite"


def backfill(vitality_path: str | Path, catalogue_path: str | Path) -> dict[str, int]:
    store = VitalityStore(vitality_path)
    with store.connect() as connection:
        rows = connection.execute(
            """SELECT company_id, state, evidence_kind
            FROM company_vitality WHERE country=''"""
        ).fetchall()
    if not rows:
        return {"updated": 0, "scheduled": 0}

    metadata = {
        str(row["company_id"]): (str(row["state"]), str(row["evidence_kind"] or ""))
        for row in rows
    }
    with duckdb.connect(str(catalogue_path), read_only=True) as connection:
        connection.execute("SET memory_limit = '256MB'")
        connection.execute("SET threads = 2")
        connection.execute("CREATE TEMP TABLE vitality_ids(id VARCHAR)")
        connection.executemany(
            "INSERT INTO vitality_ids VALUES (?)", [(company_id,) for company_id in metadata]
        )
        countries = [
            (str(company_id), normalize_market(country))
            for company_id, country in connection.execute(
                """SELECT company.id, company.country
                FROM companies AS company
                JOIN vitality_ids AS selected ON selected.id=company.id"""
            ).fetchall()
        ]

    now_text = iso_at()
    updated = 0
    scheduled = 0
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for company_id, country in countries:
            if not country:
                continue
            state, evidence_kind = metadata[company_id]
            connection.execute(
                "UPDATE company_vitality SET country=? WHERE company_id=?",
                (country, company_id),
            )
            connection.execute(
                """UPDATE vitality_queue SET country=?, priority=MIN(priority, ?)
                WHERE company_id=?""",
                (country, refresh_priority(state, country, evidence_kind), company_id),
            )
            updated += 1
            if (
                country in LEGAL_EVIDENCE_MARKETS
                and state in {"active_verified", "recently_observed"}
                and evidence_kind != "legacy_website_identity"
            ):
                connection.execute(
                    "UPDATE company_vitality SET next_check_at=? WHERE company_id=?",
                    (now_text, company_id),
                )
                scheduled += 1
        connection.commit()
    return {"updated": updated, "scheduled": scheduled}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--vitality", default=os.getenv("COMPANY_FINDER_VITALITY_DATABASE_PATH", DEFAULT_VITALITY)
    )
    parser.add_argument(
        "--catalogue", default=os.getenv("COMPANY_FINDER_DATABASE_PATH", DEFAULT_CATALOGUE)
    )
    args = parser.parse_args()
    summary = backfill(args.vitality, args.catalogue)
    print(f"updated={summary['updated']} scheduled={summary['scheduled']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
