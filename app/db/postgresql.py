"""PostgreSQL connection helpers for Verigo cutover tooling and dual-backend ops."""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Iterator
from urllib.parse import urlparse

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional until cutover deps installed
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]


def require_psycopg() -> None:
    if psycopg is None:
        raise RuntimeError(
            "psycopg is required for PostgreSQL support. "
            "Install with: pip install 'psycopg[binary]>=3.1,<4'"
        )


def resolve_database_url(
    explicit: str | None = None,
    *,
    env: dict[str, str] | None = None,
) -> str:
    """Resolve DSN without printing it. Prefer explicit, then env vars."""
    if explicit and explicit.strip():
        return explicit.strip()
    source = env if env is not None else os.environ
    for key in ("VERIGO_DATABASE_URL", "POSTGRES_DSN", "DATABASE_URL"):
        value = (source.get(key) or "").strip()
        if value:
            return value
    raise RuntimeError(
        "PostgreSQL DSN not configured. Set VERIGO_DATABASE_URL or POSTGRES_DSN."
    )


def dsn_uses_local_tunnel(dsn: str) -> bool:
    try:
        parsed = urlparse(dsn)
    except Exception:
        return "127.0.0.1:15432" in dsn or "localhost:15432" in dsn
    host = (parsed.hostname or "").lower()
    port = parsed.port or 5432
    return host in {"127.0.0.1", "localhost"} and port == 15432


def connect(
    dsn: str | None = None,
    *,
    autocommit: bool = False,
    connect_timeout: int = 15,
    dict_rows: bool = True,
):
    require_psycopg()
    url = resolve_database_url(dsn)
    # Force UTC session timezone so timestamptz digests stay stable even when
    # the host OS timezone (e.g. Asia/Beijing) is unknown to PostgreSQL.
    kwargs: dict = {
        "connect_timeout": connect_timeout,
        "options": "-c TimeZone=UTC",
    }
    if dict_rows:
        kwargs["row_factory"] = dict_row
    conn = psycopg.connect(url, **kwargs)
    conn.autocommit = autocommit
    return conn


_POOL_LOCK = None
_POOLS: dict = {}
_POOL_MAX_IDLE = 8


def _pool_state():
    global _POOL_LOCK, _POOLS
    import threading

    if _POOL_LOCK is None:
        _POOL_LOCK = threading.Lock()
    return _POOL_LOCK, _POOLS


def _connection_alive(conn) -> bool:
    if conn is None or getattr(conn, "closed", False):
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return False


def acquire_connection(
    dsn: str | None = None,
    *,
    autocommit: bool = True,
    connect_timeout: int = 15,
    dict_rows: bool = False,
):
    """Reuse idle connections; discard sockets killed by the SSH tunnel."""
    from collections import deque

    url = resolve_database_url(dsn)
    key = (url, autocommit, dict_rows)
    lock, pools = _pool_state()
    with lock:
        idle = pools.setdefault(key, deque())
        while idle:
            conn = idle.popleft()
            if _connection_alive(conn):
                return conn, key
    last_error = None
    for attempt in range(3):
        try:
            conn = connect(
                url,
                autocommit=autocommit,
                connect_timeout=connect_timeout,
                dict_rows=dict_rows,
            )
            return conn, key
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.4 * (attempt + 1))
    raise last_error


def release_connection(key, conn) -> None:
    if conn is None or getattr(conn, "closed", False):
        return
    try:
        if not conn.autocommit:
            conn.rollback()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return
    from collections import deque

    lock, pools = _pool_state()
    with lock:
        idle = pools.setdefault(key, deque())
        if len(idle) < _POOL_MAX_IDLE:
            idle.append(conn)
            return
    try:
        conn.close()
    except Exception:
        pass


@contextmanager
def connection(dsn: str | None = None, *, autocommit: bool = False) -> Iterator:
    conn = connect(dsn, autocommit=autocommit)
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


def write_rollback_probe(dsn: str | None = None) -> bool:
    """SAVEPOINT write probe used by monitor and preflight."""
    with connection(dsn, autocommit=True) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS ok")
                row = cur.fetchone()
                if not row or int(row["ok"]) != 1:
                    return False
                # Nested savepoint + rollback keeps the probe non-persistent.
                cur.execute("SAVEPOINT verigo_write_probe")
                cur.execute("SELECT pg_backend_pid()")
                cur.execute("ROLLBACK TO SAVEPOINT verigo_write_probe")
                cur.execute("RELEASE SAVEPOINT verigo_write_probe")
        return True
