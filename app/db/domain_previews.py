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

CACHE_SCHEMA_VERSION = 7


class DomainPreviewStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        connection = connect_sqlite(settings.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        if self._ready:
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
        self._ready = True

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
            payload = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
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

    def suggestions(self, prefix: str) -> list[dict[str, Any]]:
        """Return only domains previously verified and stored in the cache."""
        self._ensure_schema()
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT domain, payload_json FROM domain_relation_cache WHERE domain NOT LIKE 'query:%'").fetchall()
        matches: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                payload = json.loads(row[1])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or payload.get("cache_version") != CACHE_SCHEMA_VERSION:
                continue
            root = str(payload.get("domain") or row[0])
            candidates = [{"domain": root, "url": payload.get("url"), "title": payload.get("title"), "legal_name": payload.get("title"), "verified": True}]
            candidates.extend(payload.get("related_domains") or [])
            for item in candidates:
                if not isinstance(item, dict) or not item.get("verified", True):
                    continue
                candidate = str(item.get("domain") or "").lower()
                stem = candidate.split(".", 1)[0]
                if candidate and stem.startswith(prefix) and candidate not in matches:
                    matches[candidate] = dict(item)
        return list(matches.values())[:24]


domain_preview_store = DomainPreviewStore()
