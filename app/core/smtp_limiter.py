from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterator

from app.config import settings


class SMTPDeliveryLimiter:
    """Coordinates SMTP pressure across all local worker processes."""

    def __init__(self, database_path: Path | None = None) -> None:
        self._database_path = database_path or settings.smtp_limiter_path
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._database_path, timeout=10, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        if self._initialized:
            return
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS smtp_leases (
                    token TEXT PRIMARY KEY,
                    mx_host TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS smtp_backoff (
                    mx_host TEXT PRIMARY KEY,
                    failures INTEGER NOT NULL DEFAULT 0,
                    blocked_until REAL NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_smtp_leases_host ON smtp_leases(mx_host)"
            )
        self._initialized = True

    @contextmanager
    def permit(self, mx_host: str, capacity: int, wait_seconds: float = 180) -> Iterator[bool]:
        self._initialize()
        host = mx_host.lower().rstrip(".")
        deadline = time.monotonic() + wait_seconds
        token: str | None = None
        while time.monotonic() < deadline:
            now = time.time()
            retry_after = 0.2
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM smtp_leases WHERE expires_at <= ?", (now,))
                backoff = connection.execute(
                    "SELECT blocked_until FROM smtp_backoff WHERE mx_host = ?", (host,)
                ).fetchone()
                active = connection.execute(
                    "SELECT COUNT(*) FROM smtp_leases WHERE mx_host = ?", (host,)
                ).fetchone()[0]
                if backoff and backoff[0] > now:
                    retry_after = min(2.0, max(0.2, backoff[0] - now))
                elif active < capacity:
                    token = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO smtp_leases(token, mx_host, expires_at) VALUES (?, ?, ?)",
                        (token, host, now + wait_seconds + 30),
                    )
                connection.commit()
            if token:
                break
            time.sleep(retry_after)

        try:
            yield token is not None
        finally:
            if token:
                with closing(self._connect()) as connection:
                    connection.execute("DELETE FROM smtp_leases WHERE token = ?", (token,))

    def record_temporary_failure(self, mx_host: str) -> None:
        self._initialize()
        host = mx_host.lower().rstrip(".")
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT failures FROM smtp_backoff WHERE mx_host = ?", (host,)
            ).fetchone()
            failures = min((row[0] if row else 0) + 1, 6)
            delay = min(60.0, 2.0 ** (failures - 1))
            connection.execute(
                """
                INSERT INTO smtp_backoff(mx_host, failures, blocked_until) VALUES (?, ?, ?)
                ON CONFLICT(mx_host) DO UPDATE SET failures=excluded.failures,
                    blocked_until=excluded.blocked_until
                """,
                (host, failures, now + delay),
            )
            connection.commit()

    def record_success(self, mx_host: str) -> None:
        self._initialize()
        host = mx_host.lower().rstrip(".")
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE smtp_backoff SET failures = 0, blocked_until = 0 WHERE mx_host = ?", (host,)
            )
