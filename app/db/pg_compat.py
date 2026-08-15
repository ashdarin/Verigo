"""SQLite-shaped connection adapter over psycopg for dual-backend stores.

Goals:
- Keep existing AuthStore/JobStore SQL mostly unchanged
- Translate ``?`` placeholders to ``%s``
- Map common SQLite control statements to PostgreSQL
- Surface IntegrityError similarly to sqlite3
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Sequence

from app.config import settings
from app.db.postgresql import connect as pg_connect, resolve_database_url

# Re-export so stores can catch one error type for unique violations.
IntegrityError = sqlite3.IntegrityError


class MappingRow(tuple):
    """Tuple that also supports sqlite3.Row-style name lookup."""

    def __new__(cls, values: Sequence[Any], names: Sequence[str]):
        obj = tuple.__new__(cls, values)
        obj._map = dict(zip(names, values))  # type: ignore[attr-defined]
        return obj

    def __getitem__(self, key):  # type: ignore[override]
        if isinstance(key, str):
            return self._map[key]
        return tuple.__getitem__(self, key)


_PLACEHOLDER_RE = re.compile(r"(?<!')\?(?!')")
_INSERT_OR_IGNORE_RE = re.compile(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\s+", re.I)
_INSERT_OR_REPLACE_RE = re.compile(r"^\s*INSERT\s+OR\s+REPLACE\s+INTO\s+", re.I)
_EQ_COLLATE_NOCASE_RE = re.compile(
    r"([A-Za-z_][\w\.]*)\s*=\s*\?\s*COLLATE\s+NOCASE", re.I
)
_LIKE_COLLATE_NOCASE_RE = re.compile(r"LIKE\s+\?\s*COLLATE\s+NOCASE", re.I)
_COLLATE_NOCASE_RE = re.compile(r"\s+COLLATE\s+NOCASE", re.I)
_BEGIN_IMMEDIATE_RE = re.compile(r"^\s*BEGIN\s+IMMEDIATE\s*$", re.I)
_PRAGMA_RE = re.compile(r"^\s*PRAGMA\b", re.I)
_SUM_EQ_RE = re.compile(
    r"COALESCE\(\s*SUM\(\s*([A-Za-z_][\w\.]*)\s*=\s*('[^']*'|\d+)\s*\)\s*,\s*0\s*\)",
    re.I,
)
_SUM_EQ_BARE_RE = re.compile(
    r"SUM\(\s*([A-Za-z_][\w\.]*)\s*=\s*('[^']*'|\d+)\s*\)",
    re.I,
)
# SQLite allows ON CONFLICT(col); PostgreSQL prefers ON CONFLICT (col).
_ON_CONFLICT_RE = re.compile(r"\bON\s+CONFLICT\s*\(", re.I)


def postgres_active() -> bool:
    """Application data lives in PostgreSQL whenever a DSN is configured.

    SQLite is no longer a runtime backend for shared app tables. A DSN in
    settings always wins so env flags cannot split traffic across two files.
    """
    url = str(getattr(settings, "database_url", "") or "").strip()
    if url:
        return True
    return bool(getattr(settings, "postgres_enabled", False))


def connect_app():
    """Open the sole application database (PostgreSQL)."""
    if postgres_active():
        return PgConnection()
    raise RuntimeError(
        "SQLite is no longer an application backend. Set VERIGO_DATABASE_URL."
    )


def rewrite_sql(sql: str) -> str:
    text = sql.strip()
    if _BEGIN_IMMEDIATE_RE.match(text):
        return "BEGIN"
    if _PRAGMA_RE.match(text):
        return "SELECT 1"
    # INSERT OR IGNORE INTO t (...) VALUES (...)
    if _INSERT_OR_IGNORE_RE.match(text):
        text = _INSERT_OR_IGNORE_RE.sub("INSERT INTO ", text, count=1)
        if "ON CONFLICT" not in text.upper():
            text = text.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    # INSERT OR REPLACE INTO t(a,b) VALUES (?,?) → upsert on first column (PK convention).
    if _INSERT_OR_REPLACE_RE.match(text):
        text = _INSERT_OR_REPLACE_RE.sub("INSERT INTO ", text, count=1)
        if "ON CONFLICT" not in text.upper():
            col_match = re.search(r"INSERT\s+INTO\s+\S+\s*\(([^)]+)\)", text, re.I)
            if col_match:
                cols = [c.strip().strip('"') for c in col_match.group(1).split(",")]
                if cols:
                    pk = cols[0]
                    assigns = ", ".join(
                        f'"{c}" = EXCLUDED."{c}"' for c in cols[1:]
                    ) or f'"{pk}" = EXCLUDED."{pk}"'
                    text = (
                        text.rstrip().rstrip(";")
                        + f' ON CONFLICT ("{pk}") DO UPDATE SET {assigns}'
                    )
    # Case-insensitive equality / LIKE
    text = _EQ_COLLATE_NOCASE_RE.sub(r"LOWER(\1) = LOWER(?)", text)
    text = _LIKE_COLLATE_NOCASE_RE.sub("ILIKE ?", text)
    text = _COLLATE_NOCASE_RE.sub("", text)
    # SQLite boolean-sum idiom used in readiness / metrics queries.
    # Prefer COUNT FILTER; for numeric 0/1 comparisons cast via CASE for PG booleans.
    def _sum_eq_to_filter(match: re.Match[str]) -> str:
        col, val = match.group(1), match.group(2)
        if val in {"0", "1"}:
            return (
                f"COALESCE(SUM(CASE WHEN {col} IS TRUE OR {col}::text = {val} "
                f"THEN 1 ELSE 0 END), 0)"
                if match.re is _SUM_EQ_RE or match.string[match.start() : match.start() + 8].upper().startswith("COALESCE")
                else f"SUM(CASE WHEN {col} IS TRUE OR {col}::text = {val} THEN 1 ELSE 0 END)"
            )
        return f"COUNT(*) FILTER (WHERE {col} = {val})"

    text = _SUM_EQ_RE.sub(
        lambda m: (
            f"COALESCE(SUM(CASE WHEN {m.group(1)} IS TRUE OR {m.group(1)}::text = {m.group(2)} THEN 1 ELSE 0 END), 0)"
            if m.group(2) in {"0", "1"}
            else f"COUNT(*) FILTER (WHERE {m.group(1)} = {m.group(2)})"
        ),
        text,
    )
    text = _SUM_EQ_BARE_RE.sub(
        lambda m: (
            f"SUM(CASE WHEN {m.group(1)} IS TRUE OR {m.group(1)}::text = {m.group(2)} THEN 1 ELSE 0 END)"
            if m.group(2) in {"0", "1"}
            else f"COUNT(*) FILTER (WHERE {m.group(1)} = {m.group(2)})"
        ),
        text,
    )
    # SQLite julianday(ts) differences → PostgreSQL epoch seconds
    text = re.sub(
        r"\(julianday\(([^)]+)\)\s*-\s*julianday\(([^)]+)\)\)\s*\*\s*86400",
        r"EXTRACT(EPOCH FROM (\1 - \2))",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"julianday\(([^)]+)\)",
        r"(EXTRACT(EPOCH FROM \1) / 86400.0)",
        text,
        flags=re.I,
    )
    # SQLite stores booleans as 0/1 integers; map literal assignments/comparisons
    # for known boolean columns so PostgreSQL boolean columns accept them.
    for col in (
        "email_verified",
        "onboarding_required",
        "stop_on_deliverable",
        "suspected_bot",
        "is_valid",
        "is_skipped",
        "is_catch_all",
        "retry_updated",
        "query_fields_ready",
        "enabled",
        "active",
        "favorite",
    ):
        text = re.sub(rf"\b{col}\s*=\s*1\b", f"{col} = TRUE", text, flags=re.I)
        text = re.sub(rf"\b{col}\s*=\s*0\b", f"{col} = FALSE", text, flags=re.I)
    # Normalize ON CONFLICT(…) → ON CONFLICT (…) for PostgreSQL readability/tools.
    text = _ON_CONFLICT_RE.sub("ON CONFLICT (", text)
    text = _PLACEHOLDER_RE.sub("%s", text)
    return text


def as_json(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    import json

    return json.loads(value)


def as_datetime(value: Any):
    from datetime import date, datetime, timezone

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def as_iso(value: Any) -> str | None:
    if value is None or value == "":
        return None
    dt = as_datetime(value)
    return dt.isoformat() if dt is not None else None


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(int(value))
    text = str(value).strip().lower()
    if text in {"1", "true", "t", "yes", "on"}:
        return True
    if text in {"0", "false", "f", "no", "off"}:
        return False
    return default


class PgCursor:
    def __init__(self, conn: "PgConnection") -> None:
        self._conn = conn
        self._cur = conn._raw.cursor()
        self._last_row: Sequence[Any] | None = None
        self.lastrowid: int | None = None
        self.rowcount: int = -1
        self.description = None

    @staticmethod
    def _adapt_param(value: Any) -> Any:
        import json

        from psycopg.types.json import Jsonb

        if isinstance(value, (dict, list)):
            return Jsonb(value)
        if isinstance(value, str) and value[:1] in "[{" and value[-1:] in "]}":
            try:
                return Jsonb(json.loads(value))
            except Exception:
                return value
        return value

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> "PgCursor":
        rewritten = rewrite_sql(sql)
        adapted = tuple(self._adapt_param(p) for p in (params or ()))
        try:
            self._cur.execute(rewritten, adapted)
        except Exception as exc:  # noqa: BLE001
            message = str(exc).lower()
            if "unique" in message or "duplicate" in message or "foreign key" in message:
                raise IntegrityError(str(exc)) from exc
            raise
        self.rowcount = self._cur.rowcount
        self.description = self._cur.description
        # best-effort lastrowid for serial columns
        self.lastrowid = None
        return self

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]) -> "PgCursor":
        rewritten = rewrite_sql(sql)
        adapted = [tuple(self._adapt_param(p) for p in row) for row in seq_of_params]
        try:
            self._cur.executemany(rewritten, adapted)
        except Exception as exc:  # noqa: BLE001
            message = str(exc).lower()
            if "unique" in message or "duplicate" in message:
                raise IntegrityError(str(exc)) from exc
            raise
        self.rowcount = self._cur.rowcount
        return self

    def _as_tuple(self, row: Any) -> tuple[Any, ...] | None:
        if row is None:
            return None
        if isinstance(row, dict):
            values = tuple(row.values())
            names = list(row.keys())
        else:
            values = tuple(row)
            names = [d[0] for d in (self._cur.description or [])] if self._cur.description else []
        if names and len(names) == len(values):
            return MappingRow(values, names)
        return values

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._as_tuple(self._cur.fetchone())

    def fetchall(self) -> list[tuple[Any, ...]]:
        rows = self._cur.fetchall()
        return [self._as_tuple(r) for r in rows if r is not None]  # type: ignore[misc]

    def fetchmany(self, size: int | None = None) -> list[tuple[Any, ...]]:
        rows = self._cur.fetchmany(size) if size is not None else self._cur.fetchmany()
        return [self._as_tuple(r) for r in rows if r is not None]  # type: ignore[misc]

    def close(self) -> None:
        self._cur.close()

    def __iter__(self) -> Iterator[tuple[Any, ...]]:
        for row in self._cur:
            converted = self._as_tuple(row)
            if converted is not None:
                yield converted


class PgConnection:
    """Minimal sqlite3.Connection-compatible surface used by Verigo stores."""

    def __init__(self, dsn: str | None = None) -> None:
        # Use tuple rows (not dict_row): anonymous multi-column aggregates would
        # otherwise collapse to a single dict key and break readiness queries.
        # Match SQLite store connections (isolation_level=None / autocommit):
        # single-statement writes used with contextlib.closing() must persist
        # without an explicit commit. Explicit BEGIN (begin_immediate) still
        # opens a real transaction until commit()/rollback().
        from app.db.postgresql import acquire_connection

        self._raw, self._pool_key = acquire_connection(
            dsn or resolve_database_url(settings.database_url or None),
            dict_rows=False,
            connect_timeout=20,
            autocommit=True,
        )
        self.row_factory = None

    def _reopen(self) -> None:
        from app.db.postgresql import acquire_connection, release_connection, resolve_database_url

        old = getattr(self, "_raw", None)
        key = getattr(self, "_pool_key", None)
        if old is not None:
            try:
                release_connection(key, old)
            except Exception:
                pass
        self._raw, self._pool_key = acquire_connection(
            resolve_database_url(settings.database_url or None),
            dict_rows=False,
            connect_timeout=20,
            autocommit=True,
        )

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> PgCursor:
        try:
            cur = PgCursor(self)
            return cur.execute(sql, params)
        except Exception as exc:
            name = type(exc).__name__
            text = str(exc).lower()
            if "closed" in text or "unexpectedly" in text or name in {
                "OperationalError",
                "InterfaceError",
            }:
                self._reopen()
                cur = PgCursor(self)
                return cur.execute(sql, params)
            raise

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]) -> PgCursor:
        cur = PgCursor(self)
        return cur.executemany(sql, seq_of_params)

    def cursor(self) -> PgCursor:
        return PgCursor(self)

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        from app.db.postgresql import release_connection

        raw = getattr(self, "_raw", None)
        key = getattr(self, "_pool_key", None)
        self._raw = None
        if raw is not None:
            release_connection(key, raw)

    def __enter__(self) -> "PgConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is not None:
                # Best-effort rollback if an explicit transaction is open.
                try:
                    self.rollback()
                except Exception:
                    pass
            else:
                # No-op when already in autocommit; commits open txn if any.
                try:
                    if not self._raw.autocommit:
                        self.commit()
                except Exception:
                    try:
                        self.rollback()
                    except Exception:
                        pass
                    raise
        finally:
            self.close()


@contextmanager
def connect_backend(dsn: str | None = None):
    """Yield either a SQLite or PostgreSQL connection based on settings."""
    if postgres_active():
        conn = PgConnection(dsn)
        try:
            yield conn
        finally:
            conn.close()
    else:
        from app.db.sqlite import connect as sqlite_connect

        conn = sqlite_connect(settings.database_path)
        try:
            yield conn
        finally:
            conn.close()


def dialect_sum_eq(column: str, value_sql: str) -> str:
    """Portable conditional count expression for readiness queries."""
    if postgres_active():
        return f"COUNT(*) FILTER (WHERE {column} = {value_sql})"
    return f"COALESCE(SUM({column} = {value_sql}), 0)"


def dialect_nocase_eq(column: str, placeholder: str = "?") -> str:
    if postgres_active():
        return f"LOWER({column}) = LOWER({placeholder})"
    return f"{column} = {placeholder} COLLATE NOCASE"
