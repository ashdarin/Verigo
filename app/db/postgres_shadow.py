"""Value normalization between SQLite production rows and PostgreSQL types."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from app.db.postgres_schema import ColumnDef, TableDef


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        # Keep opaque strings out of timestamptz columns by failing loudly.
        raise ValueError(f"cannot parse timestamp value: {value!r}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(int(value))
    text = str(value).strip().lower()
    if text in {"1", "true", "t", "yes", "on"}:
        return True
    if text in {"0", "false", "f", "no", "off"}:
        return False
    raise ValueError(f"cannot parse boolean value: {value!r}")


def _parse_json(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    return json.loads(str(value))


def _strip_sql_string_literal(text: str) -> str:
    """Unwrap a SQL string literal used in DEFAULT expressions."""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        quote = text[0]
        inner = text[1:-1]
        if quote == "'":
            return inner.replace("''", "'")
        return inner.replace('\\"', '"')
    return text


def parse_column_default(column: ColumnDef) -> Any:
    """Parse a ColumnDef.default expression into a Python value."""
    raw = column.default
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    # Allow casts like '[]'::jsonb or 0::bigint.
    if "::" in text:
        text = text.split("::", 1)[0].strip()
    pg_type = column.type.lower()
    if pg_type == "boolean":
        return _parse_bool(text)
    if pg_type == "jsonb":
        return json.loads(_strip_sql_string_literal(text))
    if pg_type in {"bigint", "integer", "smallint"}:
        return int(_strip_sql_string_literal(text))
    if pg_type in {"double precision", "real", "numeric"}:
        return float(_strip_sql_string_literal(text))
    if pg_type == "timestamptz":
        return _parse_datetime(_strip_sql_string_literal(text))
    # text / other
    return _strip_sql_string_literal(text)


def _default_for_column(column: ColumnDef) -> Any:
    """Apply schema DEFAULT when SQLite is missing NULL-ish NOT NULL cells.

    Older SQLite snapshots often lack columns that later gained NOT NULL DEFAULT
    in the explicit PG schema. PostgreSQL fills those via DEFAULT on insert (or
    when the column is omitted). Digest/migrate must apply the same value or
    content digests diverge while counts and key digests still match.
    """
    if column.nullable or column.default is None:
        return None
    return parse_column_default(column)


def coerce_sqlite_value(column: ColumnDef, value: Any) -> Any:
    """Convert a SQLite cell into a Python object suitable for psycopg + PG type."""
    coerced = _coerce_sqlite_value_raw(column, value)
    if coerced is None:
        return _default_for_column(column)
    return coerced


def _coerce_sqlite_value_raw(column: ColumnDef, value: Any) -> Any:
    if value is None:
        return None
    pg_type = column.type.lower()
    if pg_type == "boolean":
        return _parse_bool(value)
    if pg_type == "timestamptz":
        return _parse_datetime(value)
    if pg_type == "jsonb":
        return _parse_json(value)
    if pg_type in {"bigint", "integer", "smallint"}:
        if value == "":
            return None
        return int(value)
    if pg_type in {"double precision", "real", "numeric"}:
        if value == "":
            return None
        return float(value)
    if pg_type == "bytea":
        if isinstance(value, memoryview):
            return bytes(value)
        if isinstance(value, str):
            return value.encode("utf-8")
        return value
    # text and other
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _canonical_json(value: Any) -> Any:
    """Recursively canonicalize JSON for stable cross-backend digests."""
    if value is None:
        return None
    if isinstance(value, dict):
        # Sort keys as strings so mixed key types from odd payloads stay stable.
        return {
            str(k): _canonical_json(value[k])
            for k in sorted(value.keys(), key=lambda item: str(item))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_json(v) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return str(value)
        # 1.0 and 1 should not diverge after JSON/jsonb round-trips.
        if value.is_integer() and abs(value) < 2**53:
            return int(value)
        # Collapse float formatting noise (e.g. 0.1+0.2 style leftovers).
        return float(format(value, ".12g"))
    if isinstance(value, Decimal):
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return str(number)
        if number.is_integer() and abs(number) < 2**53:
            return int(number)
        return float(format(number, ".12g"))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc).replace(microsecond=0)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, date):
        return value.isoformat()
    return value


def normalize_for_digest(column: ColumnDef, value: Any) -> str:
    """Stable string form for cross-backend content digests."""
    # Always coerce through the same path so SQLite strings and PG natives match.
    try:
        coerced = coerce_sqlite_value(column, value)
    except Exception:
        coerced = value
    if coerced is None:
        return "\\N"
    pg_type = column.type.lower()
    if pg_type == "boolean":
        return "1" if coerced else "0"
    if pg_type == "timestamptz":
        dt = _parse_datetime(coerced)
        if dt is None:
            return "\\N"
        # Drop sub-second noise: SQLite often stores whole seconds while
        # PostgreSQL may return microsecond timestamps after round-trip.
        dt = dt.astimezone(timezone.utc).replace(microsecond=0)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if pg_type == "jsonb":
        payload = coerced
        if isinstance(payload, str):
            payload = json.loads(payload)
        payload = _canonical_json(payload)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if pg_type in {"double precision", "real", "numeric"}:
        number = float(coerced)
        if math.isnan(number) or math.isinf(number):
            return str(number)
        # Avoid -0.0 vs 0.0 and long float noise.
        return format(number, ".12g")
    if isinstance(coerced, Decimal):
        return format(coerced, "f")
    if isinstance(coerced, (bytes, bytearray, memoryview)):
        return hashlib.sha256(bytes(coerced)).hexdigest()
    # Optional text columns: treat blank as null for digest stability.
    if pg_type == "text" and coerced == "":
        return "\\N"
    return str(coerced)


def _looks_like_pg_native(column: ColumnDef, value: Any) -> bool:
    pg_type = column.type.lower()
    if value is None:
        return True
    if pg_type == "boolean" and isinstance(value, bool):
        return True
    if pg_type == "timestamptz" and isinstance(value, (datetime, date)):
        return True
    if pg_type == "jsonb" and isinstance(value, (dict, list)):
        return True
    return False


def primary_key_values(table: TableDef, row: dict[str, Any]) -> tuple[Any, ...]:
    if table.primary_key:
        return tuple(row[name] for name in table.primary_key)
    # no PK: all columns in declaration order
    return tuple(row[col.name] for col in table.columns)


def row_digest(table: TableDef, row: dict[str, Any]) -> str:
    parts: list[str] = []
    for column in table.columns:
        parts.append(f"{column.name}={normalize_for_digest(column, row.get(column.name))}")
    material = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def table_content_digest(table: TableDef, rows: list[dict[str, Any]]) -> str:
    digests = sorted(row_digest(table, row) for row in rows)
    material = "\n".join(digests).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def table_key_digest(table: TableDef, rows: list[dict[str, Any]]) -> str:
    keys = []
    for row in rows:
        key = primary_key_values(table, row)
        # normalize each key part through column metadata when possible
        normalized: list[str] = []
        if table.primary_key:
            colmap = {c.name: c for c in table.columns}
            for name, value in zip(table.primary_key, key, strict=True):
                normalized.append(normalize_for_digest(colmap[name], value))
        else:
            for column, value in zip(table.columns, key, strict=True):
                normalized.append(normalize_for_digest(column, value))
        keys.append("|".join(normalized))
    keys.sort()
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()
