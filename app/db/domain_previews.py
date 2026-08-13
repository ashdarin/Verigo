"""Persistent cache for public domain relation previews."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.db.sqlite import connect as connect_sqlite

# Relation payloads from v12 may contain suffix-guessed domains. Keep the
# indexed suggestions, but force those payloads through the new evidence check.
CACHE_SCHEMA_VERSION = 13

# Fast, evidence-backed seed entries used before a domain has been searched in
# this installation. These are real public domains; they are suggestions only,
# and never imply that a related country site exists.
SUGGESTION_SEEDS: tuple[tuple[str, str, str, int], ...] = (
    ("paypal.com", "PayPal", "seed", 10),
    ("pinterest.com", "Pinterest", "seed", 20),
    ("philips.com", "Philips", "seed", 30),
    ("porsche.com", "Porsche", "seed", 40),
    ("porsche.de", "Porsche", "seed", 140),
    ("puma.com", "PUMA", "seed", 50),
    ("proton.me", "Proton", "seed", 60),
    ("bosch.com", "Robert Bosch GmbH", "seed", 100),
    ("bosch.de", "Robert Bosch GmbH", "seed", 101),
    ("bosch.nl", "Robert Bosch B.V.", "seed", 102),
    ("bosch.fr", "Robert Bosch (France) SAS", "seed", 103),
    ("dieseltechnic.com", "Diesel Technic SE", "seed", 110),
    ("dieseltechnic.de", "Diesel Technic SE", "seed", 111),
    ("dieseltechnic.fr", "Diesel Technic France SARL", "seed", 112),
    ("dieseltechnic.it", "Diesel Technic Italia S.R.L.", "seed", 113),
    ("dieseltechnic.co.uk", "Diesel Technic UK & Ireland LTD.", "seed", 114),
    ("dieseltechnic.sg", "Diesel Technic Asia Pacific Pte Ltd", "seed", 115),
    ("dieseltechnic.ae", "Diesel Technic (M.E.) FZE", "seed", 116),
)
SEED_LEGAL_NAMES = {
    "bosch.com": "Robert Bosch GmbH",
    "bosch.de": "Robert Bosch GmbH",
    "bosch.nl": "Robert Bosch B.V.",
    "bosch.fr": "Robert Bosch (France) SAS",
    "dieseltechnic.com": "Diesel Technic SE",
    "dieseltechnic.de": "Diesel Technic SE",
    "dieseltechnic.fr": "Diesel Technic France SARL",
    "dieseltechnic.it": "Diesel Technic Italia S.R.L.",
    "dieseltechnic.co.uk": "Diesel Technic UK & Ireland LTD.",
    "dieseltechnic.sg": "Diesel Technic Asia Pacific Pte Ltd",
    "dieseltechnic.ae": "Diesel Technic (M.E.) FZE",
}


class DomainPreviewStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._ready = False

    def _connect(self):
        from app.db.pg_compat import connect_app

        return connect_app()

    def _ensure_schema(self) -> None:
        if self._ready:
            return
        from app.db.pg_compat import postgres_active

        if postgres_active():
            self._ready = True
            return
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS domain_relation_cache (
                    domain TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    discovered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_domain_relation_cache_updated ON domain_relation_cache(updated_at DESC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS domain_suggestion_index (
                    domain TEXT PRIMARY KEY,
                    stem TEXT NOT NULL,
                    title TEXT,
                    legal_name TEXT,
                    url TEXT NOT NULL,
                    logo_url TEXT,
                    verified INTEGER NOT NULL DEFAULT 1,
                    evidence TEXT NOT NULL DEFAULT 'cache',
                    rank INTEGER NOT NULL DEFAULT 1000,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_domain_suggestion_stem ON domain_suggestion_index(stem, rank, domain)"
            )
            now = datetime.now(timezone.utc).isoformat()
            for domain, title, evidence, rank in SUGGESTION_SEEDS:
                stem = domain.split(".", 1)[0]
                connection.execute(
                    """
                    INSERT INTO domain_suggestion_index
                        (domain, stem, title, legal_name, url, logo_url, verified, evidence, rank, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    ON CONFLICT(domain) DO UPDATE SET
                        title=COALESCE(domain_suggestion_index.title, excluded.title),
                        legal_name=COALESCE(domain_suggestion_index.legal_name, excluded.legal_name),
                        url=excluded.url, logo_url=excluded.logo_url,
                        evidence=CASE WHEN domain_suggestion_index.evidence = 'cache' THEN excluded.evidence ELSE domain_suggestion_index.evidence END,
                        rank=excluded.rank, updated_at=excluded.updated_at
                    """,
                    (domain, stem, title, SEED_LEGAL_NAMES.get(domain), f"https://{domain}", f"https://logos.hunter.io/{domain}", evidence, rank, now),
                )
            # Backfill the index once for relation payloads written by older
            # versions. Prefix requests never need to decode the full payload.
            rows = connection.execute(
                "SELECT domain, payload_json FROM domain_relation_cache WHERE domain NOT LIKE 'query:%'"
            ).fetchall()
            for row in rows:
                self._index_payload(connection, str(row[0]), row[1], now)
        self._ready = True

    @staticmethod
    def _index_item(connection: sqlite3.Connection, item: dict[str, Any], now: str, rank: int = 500) -> None:
        domain = str(item.get("domain") or "").strip().lower()
        if not domain or "." not in domain or item.get("verified", True) is False:
            return
        stem = domain.split(".", 1)[0]
        title = item.get("legal_name") or item.get("title")
        connection.execute(
            """
            INSERT INTO domain_suggestion_index
                (domain, stem, title, legal_name, url, logo_url, verified, evidence, rank, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(domain) DO UPDATE SET
                title=COALESCE(excluded.title, domain_suggestion_index.title),
                legal_name=COALESCE(excluded.legal_name, domain_suggestion_index.legal_name),
                url=excluded.url, logo_url=excluded.logo_url,
                evidence=excluded.evidence, rank=MIN(domain_suggestion_index.rank, excluded.rank), updated_at=excluded.updated_at
            """,
            (domain, stem, title, item.get("legal_name"), item.get("url") or f"https://{domain}",
             item.get("logo_url") or f"https://logos.hunter.io/{domain}", str(item.get("evidence") or "cache"), rank, now),
        )

    def _index_payload(self, connection: sqlite3.Connection, cache_key: str, serialized: str, now: str) -> None:
        try:
            payload = json.loads(serialized)
        except (TypeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict) or payload.get("cache_version") != CACHE_SCHEMA_VERSION:
            return
        self._index_item(connection, payload, now, 300)
        for item in payload.get("related_domains") or []:
            if isinstance(item, dict):
                self._index_item(connection, item, now, 400)

    def get(self, domain: str) -> dict[str, Any] | None:
        self._ensure_schema()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM domain_relation_cache WHERE domain = ?",
                (domain,),
            ).fetchone()
        if row is None:
            return None
        try:
            from app.db.pg_compat import as_json

            payload = as_json(row[0], default=None)
        except (TypeError, json.JSONDecodeError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("cache_version") != CACHE_SCHEMA_VERSION:
            return None
        return payload

    def put(self, domain: str, payload: dict[str, Any]) -> None:
        self._ensure_schema()
        now = datetime.now(timezone.utc).isoformat()
        payload = {**payload, "cache_version": CACHE_SCHEMA_VERSION}
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO domain_relation_cache(domain, payload_json, discovered_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET payload_json=excluded.payload_json, updated_at=excluded.updated_at
                """,
                (domain, serialized, now, now),
            )
            self._index_payload(connection, domain, serialized, now)

    def suggestions(self, prefix: str) -> list[dict[str, Any]]:
        """Return up to six evidence-backed domains using an indexed prefix query."""
        self._ensure_schema()
        prefix = prefix.strip().lower()
        if not prefix or "." in prefix:
            return []
        with closing(self._connect()) as connection:
            indexed = connection.execute(
                """
                SELECT domain, title, legal_name, url, logo_url, evidence
                FROM domain_suggestion_index
                WHERE verified = 1 AND stem LIKE ?
                ORDER BY rank ASC, domain ASC
                LIMIT 6
                """,
                (prefix + "%",),
            ).fetchall()
        return [
            {
                "domain": row[0], "title": row[1], "legal_name": row[2],
                "url": row[3], "logo_url": row[4], "verified": True, "evidence": row[5],
            }
            for row in indexed
        ]


domain_preview_store = DomainPreviewStore()
