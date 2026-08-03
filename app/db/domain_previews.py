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
        return payload if isinstance(payload, dict) else None

    def put(self, domain: str, payload: dict[str, Any]) -> None:
        self._ensure_schema()
        now = datetime.now(timezone.utc).isoformat()
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


domain_preview_store = DomainPreviewStore()
