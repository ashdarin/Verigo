"""Shared SQLite connection policy for every persistent Verigo store."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from app.config import settings


def connect(database_path: Path, *, timeout_seconds: float = 30.0) -> sqlite3.Connection:
    """Open a WAL connection with consistent integrity and lock-wait settings."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=timeout_seconds, isolation_level=None)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(f"PRAGMA busy_timeout={settings.sqlite_busy_timeout_ms}")
    return connection


def begin_immediate(connection: sqlite3.Connection) -> None:
    """Acquire SQLite's write lock with bounded retry for transient contention."""
    attempts = settings.sqlite_write_retry_attempts
    for attempt in range(attempts):
        try:
            connection.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            locked = "locked" in str(exc).lower() or "busy" in str(exc).lower()
            if not locked or attempt + 1 >= attempts:
                raise
            time.sleep(settings.sqlite_write_retry_delay_ms * (2 ** attempt) / 1000)
