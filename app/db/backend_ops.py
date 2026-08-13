"""Backend-aware operational helpers for release, monitor, and cutover scripts.

These helpers intentionally use small, well-scoped SQL so deploy scripts do not
hard-code SQLite paths after the PostgreSQL cutover.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings
from app.db.sqlite import begin_immediate, connect as sqlite_connect


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def postgres_enabled(
    *,
    env: dict[str, str] | None = None,
    settings_obj: Any = None,
) -> bool:
    source = env if env is not None else os.environ
    cfg = settings_obj or settings
    url = (
        source.get("VERIGO_DATABASE_URL")
        or source.get("POSTGRES_DSN")
        or getattr(cfg, "database_url", "")
        or ""
    ).strip()
    # A live DSN always means PostgreSQL. Never fall back to SQLite beside it.
    if url:
        return True
    if source.get("VERIGO_POSTGRES_ENABLED") is not None:
        return str(source.get("VERIGO_POSTGRES_ENABLED", "")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    return bool(getattr(cfg, "postgres_enabled", False))


def database_url(env: dict[str, str] | None = None) -> str:
    source = env if env is not None else os.environ
    return (
        source.get("VERIGO_DATABASE_URL")
        or source.get("POSTGRES_DSN")
        or getattr(settings, "database_url", "")
        or ""
    ).strip()


def sqlite_path(env: dict[str, str] | None = None) -> Path:
    source = env if env is not None else os.environ
    raw = source.get("VERIGO_DATABASE_PATH")
    if raw:
        return Path(raw)
    return Path(settings.database_path)


def set_service_mode(mode: str, *, env: dict[str, str] | None = None) -> dict[str, Any]:
    if mode not in {"active", "draining"}:
        raise ValueError(f"unsupported service mode: {mode}")
    if postgres_enabled(env=env):
        return _set_service_mode_postgres(mode, env=env)
    return _set_service_mode_sqlite(mode, env=env)


def _set_service_mode_sqlite(mode: str, *, env: dict[str, str] | None = None) -> dict[str, Any]:
    path = sqlite_path(env=env)
    now = utc_now_iso()
    with sqlite_connect(path) as connection:
        begin_immediate(connection)
        connection.execute(
            """CREATE TABLE IF NOT EXISTS service_state (
                name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"""
        )
        connection.execute(
            """INSERT INTO service_state(name, value, updated_at)
               VALUES ('verification_mode', ?, ?)
               ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (mode, now),
        )
        connection.commit()
    return {"backend": "sqlite", "service_mode": mode, "database": str(path)}


def _set_service_mode_postgres(mode: str, *, env: dict[str, str] | None = None) -> dict[str, Any]:
    from app.db.postgresql import connection as pg_connection, resolve_database_url

    dsn = resolve_database_url(database_url(env=env) or None)
    now = datetime.now(timezone.utc)
    with pg_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO service_state(name, value, updated_at)
                VALUES ('verification_mode', %s, %s)
                ON CONFLICT (name) DO UPDATE
                SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
                """,
                (mode, now),
            )
    return {"backend": "postgres", "service_mode": mode, "database": "postgres"}


def active_job_count(*, env: dict[str, str] | None = None) -> int:
    if postgres_enabled(env=env):
        from app.db.postgresql import connection as pg_connection, resolve_database_url

        dsn = resolve_database_url(database_url(env=env) or None)
        with pg_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued', 'running')"
                )
                return int(cur.fetchone()["n"])
    with sqlite_connect(sqlite_path(env=env)) as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')"
            ).fetchone()[0]
        )


def active_targets(*, env: dict[str, str] | None = None) -> list[str]:
    sql = """
        SELECT DISTINCT execution_target FROM jobs
        WHERE status IN ('queued', 'running') AND execution_target != 'aggregate'
        ORDER BY execution_target
    """
    if postgres_enabled(env=env):
        from app.db.postgresql import connection as pg_connection, resolve_database_url

        dsn = resolve_database_url(database_url(env=env) or None)
        with pg_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                return [str(row["execution_target"]) for row in cur.fetchall()]
    with sqlite_connect(sqlite_path(env=env)) as connection:
        return [str(row[0]) for row in connection.execute(sql).fetchall()]


def drain_progress_marker(*, env: dict[str, str] | None = None) -> str:
    sqlite_sql = """
        SELECT j.status, j.execution_target, COUNT(*) AS jobs,
               COALESCE(MAX(j.heartbeat_at), ''),
               COALESCE(MAX(r.updated_at), '')
        FROM jobs j
        LEFT JOIN job_results r ON r.job_id = j.id
        WHERE j.status IN ('queued', 'running')
        GROUP BY j.status, j.execution_target
        ORDER BY j.status, j.execution_target
    """
    if postgres_enabled(env=env):
        from app.db.postgresql import connection as pg_connection, resolve_database_url

        dsn = resolve_database_url(database_url(env=env) or None)
        with pg_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT j.status AS status,
                           j.execution_target AS execution_target,
                           COUNT(*) AS jobs,
                           COALESCE(MAX(j.heartbeat_at)::text, '') AS max_heartbeat,
                           COALESCE(MAX(r.updated_at)::text, '') AS max_result_updated
                    FROM jobs j
                    LEFT JOIN job_results r ON r.job_id = j.id
                    WHERE j.status IN ('queued', 'running')
                    GROUP BY j.status, j.execution_target
                    ORDER BY j.status, j.execution_target
                    """
                )
                rows = [
                    (
                        row["status"],
                        row["execution_target"],
                        row["jobs"],
                        row["max_heartbeat"],
                        row["max_result_updated"],
                    )
                    for row in cur.fetchall()
                ]
        return "|".join(":".join(str(value) for value in row) for row in rows)
    with sqlite_connect(sqlite_path(env=env)) as connection:
        rows = connection.execute(sqlite_sql).fetchall()
    return "|".join(":".join(str(value) for value in row) for row in rows)


def database_write_probe(*, env: dict[str, str] | None = None) -> bool:
    if postgres_enabled(env=env):
        from app.db.postgresql import resolve_database_url, write_rollback_probe

        dsn = resolve_database_url(database_url(env=env) or None)
        return write_rollback_probe(dsn)
    path = sqlite_path(env=env)
    with sqlite_connect(path) as connection:
        begin_immediate(connection)
        connection.rollback()
    return True


def health_summary(*, env: dict[str, str] | None = None) -> dict[str, Any]:
    if postgres_enabled(env=env):
        from app.db.postgresql import connection as pg_connection, resolve_database_url

        dsn = resolve_database_url(database_url(env=env) or None)
        with pg_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT value FROM service_state WHERE name='verification_mode'"
                )
                row = cur.fetchone()
                mode = row["value"] if row else "active"
                cur.execute(
                    "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status ORDER BY status"
                )
                by_status = {r["status"]: int(r["n"]) for r in cur.fetchall()}
                cur.execute(
                    "SELECT COUNT(*) AS n FROM job_leases WHERE completed_at IS NULL"
                )
                active_leases = int(cur.fetchone()["n"])
        return {
            "backend": "postgres",
            "service_mode": mode,
            "jobs_by_status": by_status,
            "active_job_leases": active_leases,
        }

    path = sqlite_path(env=env)
    with sqlite_connect(path) as connection:
        row = connection.execute(
            "SELECT value FROM service_state WHERE name='verification_mode'"
        ).fetchone()
        mode = row[0] if row else "active"
        by_status = {
            status: count
            for status, count in connection.execute(
                "SELECT status, COUNT(*) FROM jobs GROUP BY status ORDER BY status"
            )
        }
        try:
            active_leases = connection.execute(
                "SELECT COUNT(*) FROM job_leases WHERE completed_at IS NULL"
            ).fetchone()[0]
        except Exception:
            active_leases = 0
    return {
        "backend": "sqlite",
        "service_mode": mode,
        "jobs_by_status": by_status,
        "active_job_leases": int(active_leases),
    }
