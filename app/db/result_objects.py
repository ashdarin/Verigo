from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.db.pg_compat import PgConnection, as_iso, as_json, postgres_active
from app.db.sqlite import connect as connect_sqlite


logger = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ResultObjectStore:
    """Account-owned saved result objects and reusable email lists."""

    def _connect(self):
        from app.db.pg_compat import connect_app

        connection = connect_app()
        if not isinstance(connection, PgConnection):
            connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        if postgres_active():
            return
        with closing(self._connect()) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS result_objects (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, task_id TEXT NOT NULL, result_index INTEGER NOT NULL, email TEXT NOT NULL, status TEXT NOT NULL, verification_method TEXT, server_response TEXT, confidence TEXT NOT NULL DEFAULT 'unknown', source TEXT NOT NULL, created_at TEXT NOT NULL, supersedes_result_id TEXT, metadata_json TEXT NOT NULL DEFAULT '{}', UNIQUE(owner_id, task_id, result_index))")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_result_objects_owner ON result_objects(owner_id, created_at DESC)")
            connection.execute("CREATE TABLE IF NOT EXISTS lists (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_lists_owner ON lists(owner_id, archived_at, updated_at DESC)")
            connection.execute("CREATE TABLE IF NOT EXISTS list_items (list_id TEXT NOT NULL, result_id TEXT NOT NULL, added_at TEXT NOT NULL, added_from TEXT NOT NULL, PRIMARY KEY(list_id, result_id), FOREIGN KEY(list_id) REFERENCES lists(id) ON DELETE CASCADE, FOREIGN KEY(result_id) REFERENCES result_objects(id) ON DELETE CASCADE)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_list_items_result ON list_items(result_id)")

    @staticmethod
    def _status(result: dict[str, Any]) -> str:
        if result.get("is_catch_all") or result.get("catch_all"):
            return "catch-all"
        if result.get("deliverable") is True:
            return "deliverable"
        if result.get("deliverable") is False:
            return "undeliverable"
        if result.get("progress_state") in {"pending", "verifying"}:
            return "queued" if result.get("progress_state") == "pending" else "running"
        if result.get("progress_state") == "failed":
            return "failed"
        return "unknown"

    @classmethod
    def _row(cls, row, list_ids: list[str] | None = None) -> dict[str, Any]:
        metadata = as_json(row["metadata_json"], default={}) or {}
        created = row["created_at"]
        return {
            "id": row["id"], "email": row["email"], "status": row["status"],
            "verification_method": row["verification_method"], "server_response": row["server_response"],
            "confidence": row["confidence"], "source": row["source"], "task_id": row["task_id"],
            "created_at": as_iso(created) or created, "list_ids": list_ids or [],
            "supersedes_result_id": row["supersedes_result_id"], "metadata": metadata,
        }

    def ensure_result(self, owner_id: str, task_id: str, result_index: int, result: dict[str, Any], source: str) -> dict[str, Any]:
        self.initialize()
        email = str(result.get("email") or "").strip().lower()
        if not email:
            raise ValueError("result email is required")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM result_objects WHERE owner_id=? AND task_id=? AND result_index=?",
                (owner_id, task_id, result_index),
            ).fetchone()
            if row:
                incoming_pending = result.get("progress_state") in {"pending", "verifying"}
                if not incoming_pending:
                    connection.execute(
                        "UPDATE result_objects SET status=?, verification_method=?, server_response=?, confidence=?, metadata_json=? WHERE id=? AND owner_id=?",
                        (self._status(result), result.get("verification_method") or result.get("strategy"), result.get("smtp_result") or result.get("message"), result.get("confidence") or row["confidence"], json.dumps({k: result[k] for k in ("domain_type", "original_index", "first_name", "last_name", "domain") if k in result}, ensure_ascii=False), row["id"], owner_id),
                    )
                    row = connection.execute("SELECT * FROM result_objects WHERE id=?", (row["id"],)).fetchone()
                return self._row(row, self._list_ids(connection, row["id"]))
            result_id = uuid.uuid4().hex
            created_at = now_iso()
            previous = connection.execute(
                "SELECT id FROM result_objects WHERE owner_id=? AND lower(email)=lower(?) ORDER BY created_at DESC LIMIT 1",
                (owner_id, email),
            ).fetchone()
            connection.execute(
                """INSERT INTO result_objects(
                    id,owner_id,task_id,result_index,email,status,verification_method,
                    server_response,confidence,source,created_at,supersedes_result_id,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (result_id, owner_id, task_id, result_index, email, self._status(result),
                 result.get("verification_method") or result.get("strategy"),
                 result.get("smtp_result") or result.get("message"),
                 result.get("confidence") or "unknown", source, created_at,
                result.get("supersedes_result_id") or (previous["id"] if previous else None),
                json.dumps({k: result[k] for k in ("domain_type", "original_index", "first_name", "last_name", "domain") if k in result}, ensure_ascii=False)),
            )
            row = connection.execute("SELECT * FROM result_objects WHERE id=?", (result_id,)).fetchone()
            return self._row(row)

    @staticmethod
    def _list_ids(connection: sqlite3.Connection, result_id: str) -> list[str]:
        return [row[0] for row in connection.execute("SELECT list_id FROM list_items WHERE result_id=?", (result_id,)).fetchall()]

    def create_list(self, owner_id: str, name: str, description: str = "") -> dict[str, Any]:
        self.initialize()
        name = name.strip()
        if not name or len(name) > 120:
            raise ValueError("list name is required")
        timestamp = now_iso(); list_id = uuid.uuid4().hex
        with closing(self._connect()) as connection:
            connection.execute("INSERT INTO lists(id,owner_id,name,description,created_at,updated_at) VALUES(?,?,?,?,?,?)", (list_id, owner_id, name, description.strip()[:500], timestamp, timestamp))
            return self.get_list(owner_id, list_id, connection=connection)

    def update_list(
        self,
        owner_id: str,
        list_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Update an active list without exposing a database connection to callers."""
        self.initialize()
        with closing(self._connect()) as connection:
            current = self.get_list(owner_id, list_id, connection=connection)
            if current is None:
                raise ValueError("list not found")
            next_name = (name if name is not None else current["name"]).strip()
            if not next_name or len(next_name) > 120:
                raise ValueError("list name is required")
            next_description = str(
                description if description is not None else current["description"]
            ).strip()
            if len(next_description) > 500:
                raise ValueError("list description is too long")
            connection.execute(
                "UPDATE lists SET name=?, description=?, updated_at=? WHERE id=? AND owner_id=?",
                (next_name, next_description, now_iso(), list_id, owner_id),
            )
            return self.get_list(owner_id, list_id, connection=connection) or current

    def archive_list(self, owner_id: str, list_id: str) -> None:
        """Archive an owned active list and make it unavailable to normal queries."""
        self.initialize()
        with closing(self._connect()) as connection:
            if self.get_list(owner_id, list_id, connection=connection) is None:
                raise ValueError("list not found")
            timestamp = now_iso()
            connection.execute(
                "UPDATE lists SET archived_at=?, updated_at=? WHERE id=? AND owner_id=?",
                (timestamp, timestamp, list_id, owner_id),
            )

    def get_list(self, owner_id: str, list_id: str, *, connection=None) -> dict[str, Any] | None:
        own = connection is None
        connection = connection or self._connect()
        try:
            row = connection.execute("SELECT * FROM lists WHERE id=? AND owner_id=? AND archived_at IS NULL", (list_id, owner_id)).fetchone()
            if not row: return None
            count = connection.execute("SELECT COUNT(*) FROM list_items WHERE list_id=?", (list_id,)).fetchone()[0]
            return {"id": row["id"], "name": row["name"], "description": row["description"], "result_count": int(count), "created_at": row["created_at"], "updated_at": row["updated_at"]}
        finally:
            if own: connection.close()

    def list_lists(self, owner_id: str) -> list[dict[str, Any]]:
        self.initialize()
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT id FROM lists WHERE owner_id=? AND archived_at IS NULL ORDER BY updated_at DESC", (owner_id,)).fetchall()
            return [self.get_list(owner_id, row[0], connection=connection) for row in rows]

    def add_results(self, owner_id: str, list_id: str, result_ids: list[str], added_from: str = "history") -> dict[str, Any]:
        self.initialize()
        with closing(self._connect()) as connection:
            target = self.get_list(owner_id, list_id, connection=connection)
            if not target: raise ValueError("list not found")
            timestamp = now_iso()
            added = 0
            for result_id in dict.fromkeys(result_ids):
                if connection.execute("SELECT 1 FROM result_objects WHERE id=? AND owner_id=?", (result_id, owner_id)).fetchone():
                    cur = connection.execute("INSERT OR IGNORE INTO list_items(list_id,result_id,added_at,added_from) VALUES(?,?,?,?)", (list_id, result_id, timestamp, added_from))
                    added += cur.rowcount
            connection.execute("UPDATE lists SET updated_at=? WHERE id=? AND owner_id=?", (timestamp, list_id, owner_id))
            return {"added": added, "list": self.get_list(owner_id, list_id, connection=connection)}

    def remove_results(self, owner_id: str, list_id: str, result_ids: list[str]) -> dict[str, Any]:
        self.initialize()
        with closing(self._connect()) as connection:
            if not self.get_list(owner_id, list_id, connection=connection): raise ValueError("list not found")
            connection.executemany("DELETE FROM list_items WHERE list_id=? AND result_id=?", [(list_id, rid) for rid in set(result_ids)])
            connection.execute("UPDATE lists SET updated_at=? WHERE id=? AND owner_id=?", (now_iso(), list_id, owner_id))
            return {"removed": len(set(result_ids)), "list": self.get_list(owner_id, list_id, connection=connection)}

    def list_results(self, owner_id: str, list_id: str, offset: int = 0, limit: int = 50, status: str = "all") -> tuple[int, list[dict[str, Any]]]:
        self.initialize()
        with closing(self._connect()) as connection:
            if not self.get_list(owner_id, list_id, connection=connection): raise ValueError("list not found")
            where = "li.list_id=? AND ro.owner_id=?"; args: list[Any] = [list_id, owner_id]
            if status != "all": where += " AND ro.status=?"; args.append(status)
            total = connection.execute(f"SELECT COUNT(*) FROM list_items li JOIN result_objects ro ON ro.id=li.result_id WHERE {where}", args).fetchone()[0]
            rows = connection.execute(f"SELECT ro.* FROM list_items li JOIN result_objects ro ON ro.id=li.result_id WHERE {where} ORDER BY ro.created_at DESC LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
            return int(total), [self._row(row, self._list_ids(connection, row["id"])) for row in rows]

    def get_result(self, owner_id: str, result_id: str) -> dict[str, Any] | None:
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM result_objects WHERE id=? AND owner_id=?", (result_id, owner_id)).fetchone()
            return self._row(row, self._list_ids(connection, result_id)) if row else None

    def result_history(self, owner_id: str, result_id: str) -> dict[str, Any]:
        """Return an account-scoped result's verification history for one email."""
        self.initialize()
        with closing(self._connect()) as connection:
            result = connection.execute(
                "SELECT * FROM result_objects WHERE id=? AND owner_id=?",
                (result_id, owner_id),
            ).fetchone()
            if result is None:
                raise ValueError("result not found")
            rows = connection.execute(
                "SELECT * FROM result_objects WHERE owner_id=? AND lower(email)=lower(?) "
                "ORDER BY created_at DESC",
                (owner_id, result["email"]),
            ).fetchall()
            return {
                "email": result["email"],
                "items": [
                    self._row(row, self._list_ids(connection, row["id"]))
                    for row in rows
                ],
            }

    def recent_results(self, owner_id: str, limit: int = 8) -> list[dict[str, Any]]:
        self.initialize()
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM result_objects WHERE owner_id=? ORDER BY created_at DESC LIMIT ?", (owner_id, limit)).fetchall()
            return [self._row(row, self._list_ids(connection, row["id"])) for row in rows]

    def export_rows(self, owner_id: str, list_id: str) -> list[dict[str, Any]]:
        return self.list_results(owner_id, list_id, 0, 100000)[1]

    @staticmethod
    def source_for_job(job: Any) -> str:
        """Tag saved objects the same way the save-result API does."""
        if getattr(job, "execution_target", "") == "discovery" or getattr(job, "stop_on_deliverable", False):
            return "discovery"
        if getattr(job, "execution_target", "") == "reverify":
            return "reverify"
        emails = getattr(job, "emails", None) or []
        return "single" if len(emails) == 1 else "batch"

    def publish_completed_job(self, job: Any) -> int:
        """Persist settled rows for a user-visible completed job.

        Internal child / retry jobs are skipped; callers publish the parent
        once it itself is completed. Failures are swallowed per-row so a
        result_objects write cannot roll back job completion.
        """
        owner_id = getattr(job, "owner_id", None)
        if not owner_id or getattr(job, "parent_id", None) or getattr(job, "retry_parent_id", None):
            return 0
        source = self.source_for_job(job)
        written = 0
        for fallback_index, raw in enumerate(getattr(job, "results", None) or []):
            result = dict(raw)
            if result.get("progress_state") in {"pending", "verifying"}:
                continue
            index = int(result.get("original_index", fallback_index))
            try:
                self.ensure_result(owner_id, job.id, index, result, source)
                written += 1
            except Exception:
                logger.exception(
                    "Failed to persist result_object owner=%s job=%s index=%s",
                    owner_id,
                    getattr(job, "id", ""),
                    index,
                )
        return written


result_object_store = ResultObjectStore()
