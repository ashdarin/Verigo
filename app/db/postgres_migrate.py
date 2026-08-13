"""Shared SQLite -> PostgreSQL migration helpers (P0.2)."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

from app.db.postgres_schema import TableDef, create_indexes_sql, create_table_sql, require_registered
from app.db.postgres_shadow import (
    coerce_sqlite_value,
    normalize_for_digest,
    primary_key_values,
    table_content_digest,
    table_key_digest,
)
from app.db.postgresql import connection as pg_connection


def open_sqlite(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def sqlite_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(r[0]) for r in rows}


def _sqlite_select_columns(conn: sqlite3.Connection, table: TableDef) -> list[str]:
    col_names = [c.name for c in table.columns]
    existing = {
        row[1] for row in conn.execute(f'PRAGMA table_info("{table.name}")').fetchall()
    }
    return [name for name in col_names if name in existing]


def iter_sqlite_rows(conn: sqlite3.Connection, table: TableDef):
    """Yield source rows without materializing the full table."""
    col_names = [c.name for c in table.columns]
    select_cols = _sqlite_select_columns(conn, table)
    if not select_cols:
        return
    quoted = ", ".join(f'"{name}"' for name in select_cols)
    cursor = conn.execute(f'SELECT {quoted} FROM "{table.name}"')
    while True:
        batch = cursor.fetchmany(500)
        if not batch:
            break
        for row in batch:
            item = {name: None for name in col_names}
            for name in select_cols:
                item[name] = row[name]
            yield item


def fetch_sqlite_rows(conn: sqlite3.Connection, table: TableDef) -> list[dict[str, Any]]:
    return list(iter_sqlite_rows(conn, table))


def normalize_rows(table: TableDef, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    colmap = {c.name: c for c in table.columns}
    return [
        {c.name: coerce_sqlite_value(colmap[c.name], row.get(c.name)) for c in table.columns}
        for row in rows
    ]


def summarize_sqlite_table(
    conn: sqlite3.Connection,
    table: TableDef,
    *,
    level: str = "full",
) -> dict[str, Any]:
    """Stream SQLite rows into digests (memory-friendly for large tables).

    level: full | keys | counts
    """
    import hashlib

    from app.db.postgres_shadow import row_digest

    colmap = {c.name: c for c in table.columns}
    key_digests: list[str] = []
    content_digests: list[str] = []
    count = 0
    for raw in iter_sqlite_rows(conn, table):
        row = {c.name: coerce_sqlite_value(colmap[c.name], raw.get(c.name)) for c in table.columns}
        count += 1
        if level == "counts":
            continue
        if table.primary_key:
            key_parts = [
                normalize_for_digest(colmap[n], row[n]) for n in table.primary_key
            ]
        else:
            key_parts = [normalize_for_digest(c, row[c.name]) for c in table.columns]
        key_digests.append("|".join(key_parts))
        if level == "full":
            content_digests.append(row_digest(table, row))
    key_digests.sort()
    content_digests.sort()
    return {
        "table": table.name,
        "count": count,
        "key_digest": (
            hashlib.sha256("\n".join(key_digests).encode("utf-8")).hexdigest()
            if level != "counts"
            else ""
        ),
        "content_digest": (
            hashlib.sha256("\n".join(content_digests).encode("utf-8")).hexdigest()
            if level == "full"
            else ""
        ),
        "verify_level": level,
    }


def ensure_schema(pg_dsn: str, tables: Iterable[str], *, recreate: bool = False) -> None:
    with pg_connection(pg_dsn) as conn:
        with conn.cursor() as cur:
            for name in tables:
                table = require_registered(name)
                if recreate:
                    cur.execute(f'DROP TABLE IF EXISTS "{table.name}" CASCADE')
                cur.execute(create_table_sql(table))
                for stmt in create_indexes_sql(table):
                    cur.execute(stmt)


def chunks(rows: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def _upsert_sql(table: TableDef, col_names: list[str]) -> str:
    quoted_cols = ", ".join(f'"{name}"' for name in col_names)
    placeholders = ", ".join(["%s"] * len(col_names))
    if not table.primary_key:
        return f'INSERT INTO "{table.name}" ({quoted_cols}) VALUES ({placeholders})'
    conflict = ", ".join(f'"{name}"' for name in table.primary_key)
    non_pk = [name for name in col_names if name not in table.primary_key]
    if non_pk:
        assignments = ", ".join(f'"{name}" = EXCLUDED."{name}"' for name in non_pk)
        return (
            f'INSERT INTO "{table.name}" ({quoted_cols}) VALUES ({placeholders}) '
            f"ON CONFLICT ({conflict}) DO UPDATE SET {assignments}"
        )
    return (
        f'INSERT INTO "{table.name}" ({quoted_cols}) VALUES ({placeholders}) '
        f"ON CONFLICT ({conflict}) DO NOTHING"
    )


def upsert_table(
    pg_dsn: str,
    table: TableDef,
    rows: list[dict[str, Any]],
    *,
    batch_size: int,
    prune: bool,
) -> dict[str, Any]:
    return upsert_table_stream(
        pg_dsn,
        table,
        iter(rows),
        batch_size=batch_size,
        prune=prune,
        known_count=len(rows),
    )


def _adapt_pg_param(column_type: str, value: Any) -> Any:
    """Make Python values acceptable to psycopg for our explicit schema types."""
    if value is None:
        return None
    pg_type = column_type.lower()
    if pg_type == "jsonb":
        from psycopg.types.json import Jsonb

        if isinstance(value, (dict, list)):
            return Jsonb(value)
        if isinstance(value, str):
            import json

            try:
                return Jsonb(json.loads(value))
            except Exception:
                return Jsonb(value)
        return Jsonb(value)
    return value


def upsert_table_stream(
    pg_dsn: str,
    table: TableDef,
    row_iter: Iterable[dict[str, Any]],
    *,
    batch_size: int,
    prune: bool,
    known_count: int | None = None,
) -> dict[str, Any]:
    """Upsert rows from an iterator; only PK digests are retained for prune."""
    col_names = [c.name for c in table.columns]
    colmap = {c.name: c for c in table.columns}
    sql = _upsert_sql(table, col_names)
    source_key_norms: set[tuple[str, ...]] = set()
    source_rows = 0
    pruned = 0
    batch: list[tuple[Any, ...]] = []

    def flush(cur, pending: list[tuple[Any, ...]]) -> None:
        if pending:
            cur.executemany(sql, pending)
            pending.clear()

    with pg_connection(pg_dsn) as conn:
        with conn.cursor() as cur:
            if not table.primary_key:
                cur.execute(f'DELETE FROM "{table.name}"')

            for row in row_iter:
                prepared = tuple(
                    _adapt_pg_param(
                        colmap[name].type,
                        coerce_sqlite_value(colmap[name], row.get(name)),
                    )
                    for name in col_names
                )
                batch.append(prepared)
                source_rows += 1
                if table.primary_key and prune:
                    coerced = {name: prepared[i] for i, name in enumerate(col_names)}
                    key = primary_key_values(table, coerced)
                    source_key_norms.add(
                        tuple(
                            normalize_for_digest(colmap[n], key[i])
                            for i, n in enumerate(table.primary_key)
                        )
                    )
                if len(batch) >= batch_size:
                    flush(cur, batch)

            flush(cur, batch)

            if prune and table.primary_key:
                pk_select = ", ".join(f'"{n}"' for n in table.primary_key)
                cur.execute(f'SELECT {pk_select} FROM "{table.name}"')
                stale: list[tuple[Any, ...]] = []
                for trow in cur.fetchall():
                    tnorm = tuple(
                        normalize_for_digest(colmap[n], trow[n]) for n in table.primary_key
                    )
                    if tnorm not in source_key_norms:
                        stale.append(tuple(trow[n] for n in table.primary_key))
                if stale:
                    where = " AND ".join(f'"{n}" = %s' for n in table.primary_key)
                    for stale_batch in chunks(stale, batch_size):
                        cur.executemany(
                            f'DELETE FROM "{table.name}" WHERE {where}', stale_batch
                        )
                    pruned = len(stale)

            cur.execute(f'SELECT COUNT(*) AS n FROM "{table.name}"')
            target_count = int(cur.fetchone()["n"])

    return {
        "table": table.name,
        "source_rows": known_count if known_count is not None else source_rows,
        "target_rows": target_count,
        "pruned": pruned,
    }


def summarize_pg_table(
    pg_dsn: str,
    table: TableDef,
    *,
    level: str = "full",
) -> dict[str, Any]:
    """Stream PostgreSQL rows into digests without materializing all rows."""
    import hashlib

    from app.db.postgres_shadow import row_digest

    col_names = [c.name for c in table.columns]
    quoted = ", ".join(f'"{name}"' for name in col_names)
    key_digests: list[str] = []
    content_digests: list[str] = []
    count = 0
    with pg_connection(pg_dsn) as conn:
        with conn.cursor() as cur:
            if level == "counts":
                cur.execute(f'SELECT COUNT(*) AS n FROM "{table.name}"')
                count = int(cur.fetchone()["n"])
                return {
                    "table": table.name,
                    "count": count,
                    "key_digest": "",
                    "content_digest": "",
                    "verify_level": level,
                }
            cur.execute(f'SELECT {quoted} FROM "{table.name}"')
            while True:
                batch = cur.fetchmany(500)
                if not batch:
                    break
                for row in batch:
                    data = dict(row)
                    count += 1
                    if table.primary_key:
                        key_parts = [
                            normalize_for_digest(
                                next(c for c in table.columns if c.name == n), data[n]
                            )
                            for n in table.primary_key
                        ]
                    else:
                        key_parts = [
                            normalize_for_digest(c, data[c.name]) for c in table.columns
                        ]
                    key_digests.append("|".join(key_parts))
                    if level == "full":
                        content_digests.append(row_digest(table, data))
    key_digests.sort()
    content_digests.sort()
    return {
        "table": table.name,
        "count": count,
        "key_digest": hashlib.sha256("\n".join(key_digests).encode("utf-8")).hexdigest(),
        "content_digest": (
            hashlib.sha256("\n".join(content_digests).encode("utf-8")).hexdigest()
            if level == "full"
            else ""
        ),
        "verify_level": level,
    }


def fetch_pg_rows(pg_dsn: str, table: TableDef) -> list[dict[str, Any]]:
    col_names = [c.name for c in table.columns]
    quoted = ", ".join(f'"{name}"' for name in col_names)
    with pg_connection(pg_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(f'SELECT {quoted} FROM "{table.name}"')
            return [dict(row) for row in cur.fetchall()]


def summarize_table(table: TableDef, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "table": table.name,
        "count": len(rows),
        "key_digest": table_key_digest(table, rows),
        "content_digest": table_content_digest(table, rows),
    }


def financial_summary_sqlite(conn: sqlite3.Connection) -> dict[str, Any]:
    queries = {
        "users_credits": "SELECT COUNT(*), COALESCE(SUM(credits),0) FROM users",
        "credit_ledger": "SELECT COUNT(*), COALESCE(SUM(delta),0) FROM credit_ledger",
        "paid_orders": (
            "SELECT COUNT(*), COALESCE(SUM(credits),0), COALESCE(SUM(amount_fen),0) "
            "FROM payment_orders WHERE status='paid'"
        ),
        "redeemed_codes": (
            "SELECT COUNT(*), COALESCE(SUM(credits),0), COALESCE(SUM(amount_fen),0) "
            "FROM redemption_codes WHERE redeemed_at IS NOT NULL"
        ),
        "promo_remaining": (
            "SELECT COUNT(*), COALESCE(SUM(remaining_credits),0) FROM promo_credit_grants"
        ),
    }
    out: dict[str, Any] = {}
    for name, sql in queries.items():
        try:
            out[name] = [ _jsonable(v) for v in conn.execute(sql).fetchone() ]
        except sqlite3.Error as exc:
            out[name] = {"error": str(exc)}
    return out


def financial_summary_pg(pg_dsn: str) -> dict[str, Any]:
    queries = {
        "users_credits": "SELECT COUNT(*) AS c, COALESCE(SUM(credits),0) AS s FROM users",
        "credit_ledger": "SELECT COUNT(*) AS c, COALESCE(SUM(delta),0) AS s FROM credit_ledger",
        "paid_orders": (
            "SELECT COUNT(*) AS c, COALESCE(SUM(credits),0) AS s1, COALESCE(SUM(amount_fen),0) AS s2 "
            "FROM payment_orders WHERE status='paid'"
        ),
        "redeemed_codes": (
            "SELECT COUNT(*) AS c, COALESCE(SUM(credits),0) AS s1, COALESCE(SUM(amount_fen),0) AS s2 "
            "FROM redemption_codes WHERE redeemed_at IS NOT NULL"
        ),
        "promo_remaining": (
            "SELECT COUNT(*) AS c, COALESCE(SUM(remaining_credits),0) AS s "
            "FROM promo_credit_grants"
        ),
    }
    out: dict[str, Any] = {}
    with pg_connection(pg_dsn) as conn:
        with conn.cursor() as cur:
            for name, sql in queries.items():
                try:
                    cur.execute(sql)
                    row = cur.fetchone()
                    out[name] = [_jsonable(v) for v in row.values()] if row else []
                except Exception as exc:  # noqa: BLE001
                    conn.rollback()
                    out[name] = {"error": str(exc)}
    return out


def _jsonable(value: Any) -> Any:
    if hasattr(value, "as_integer_ratio") and not isinstance(value, bool):
        try:
            return int(value)
        except Exception:
            return float(value)
    return value
