"""Shared SQLite connection policy for every persistent Verigo store."""
from __future__ import annotations

import sqlite3
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
