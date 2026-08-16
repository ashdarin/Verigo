"""Persistent, explainable website vitality checks for Company Finder."""

from __future__ import annotations

import html
import ipaddress
import re
import socket
import sqlite3
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


ACTIVE_TTL = timedelta(days=30)
MODERATE_TTL = timedelta(days=7)
RETRY_TTL = timedelta(days=1)
UNCERTAIN_TTL = timedelta(days=3)
INACTIVE_TTL = timedelta(days=30)
CLAIM_TTL = timedelta(minutes=15)
MAX_QUEUE_SIZE = 50_000
MAX_RESPONSE_BYTES = 256 * 1024
USER_AGENT = "VerigoCompanyMonitor/1.0 (+https://verigo.site)"

_PARKING_PHRASES = (
    "this domain is for sale",
    "buy this domain",
    "domain may be for sale",
    "sedo domain parking",
    "afternic",
    "hugedomains.com",
    "parkingcrew",
    "dan.com domain",
)
_TRANSIENT_REASONS = frozenset({
    "dns_temporary_failure", "connection_failed", "http_5xx", "http_restricted",
    "tls_failure", "worker_error",
})
_STRONG_INACTIVE_REASONS = frozenset({
    "invalid_domain", "nxdomain", "non_public_address", "parked_domain",
})
_TOKEN_RE = re.compile(r"[a-z0-9\u4e00-\u9fff]+", re.IGNORECASE)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_NOISE_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_COMMON_COMPANY_TOKENS = frozenset({
    "ag", "and", "bv", "company", "co", "corporation", "corp", "gmbh", "group",
    "holding", "holdings", "inc", "international", "limited", "llc", "ltd", "nv",
    "oy", "plc", "sa", "sarl", "spa", "the",
})

_MARKET_ALIASES = {
    "al": "albania", "ar": "argentina", "au": "australia", "be": "belgium",
    "br": "brazil", "cn": "china", "de": "germany", "es": "spain", "fr": "france",
    "gb": "united kingdom", "in": "india", "it": "italy", "jp": "japan",
    "kr": "south korea", "mx": "mexico", "nl": "netherlands", "uk": "united kingdom",
}
_PRIORITY_MARKETS = frozenset({
    "australia", "belgium", "brazil", "canada", "china", "france", "germany", "india",
    "italy", "japan", "mexico", "netherlands", "south korea", "spain", "switzerland",
    "united kingdom", "united states",
})
_MARKET_LEGAL_PATTERNS = {
    "albania": (r"\bnipt\b", r"qendra komb[eë]tare e biznesit"),
    "argentina": (r"\bcuit\b", r"inspecci[oó]n general de justicia"),
    "australia": (r"\babn\s*(?:no\.?\s*)?\d", r"\bacn\s*\d", r"australian business number"),
    "belgium": (r"ondernemingsnummer", r"num[eé]ro d['’]entreprise", r"\b(?:kbo|bce)\s*\d"),
    "brazil": (r"\bcnpj\b",),
    "china": (r"统一社会信用代码", r"icp备案", r"\b[a-z津京沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼]icp备"),
    "france": (r"\bsiret\b", r"\bsiren\b", r"mentions l[eé]gales", r"registre du commerce"),
    "germany": (r"\bimpressum\b", r"handelsregister", r"gesch[aä]ftsf[uü]hrer", r"ust[.-]?id"),
    "india": (r"corporate identity number", r"\bgstin\b", r"\bcin\s*[:#]"),
    "italy": (r"partita iva", r"registro imprese"),
    "japan": (r"法人番号", r"会社概要", r"特定商取引法"),
    "mexico": (r"registro federal de contribuyentes", r"\brfc\s*[:#]"),
    "netherlands": (r"kamer van koophandel", r"\bkvk\s*(?:nummer)?\s*\d"),
    "south korea": (r"사업자등록번호", r"법인등록번호"),
    "spain": (r"registro mercantil", r"\bc\.?i\.?f\.\b", r"aviso legal"),
    "united kingdom": (r"companies house", r"company (?:registration )?number", r"registered in (?:england|scotland|wales)"),
}
LEGAL_EVIDENCE_MARKETS = frozenset(_MARKET_LEGAL_PATTERNS)

SEARCH_PRIORITY_BASE = 10
RECENT_RECHECK_PRIORITY = 25
LEGACY_RECHECK_PRIORITY = 35
ACTIVE_RECHECK_PRIORITY = 50
UNCERTAIN_RECHECK_PRIORITY = 80
INACTIVE_RECHECK_PRIORITY = 120
SAMPLE_PRIORITY = 200

_PUBLIC_STATES = frozenset({"active_verified", "recently_observed"})
_QUALITY_STATES = ("active_verified", "recently_observed", "uncertain", "inactive")
_QUALITY_EVIDENCE = (
    "official_website_legal", "official_website_title", "official_website_content",
    "official_website_domain", "legacy_website_identity", "none",
)
_QUALITY_SOURCES = ("user_search", "scheduled_refresh", "daily_sample")
_DURATION_BUCKETS_MS = (
    100, 250, 500, 1_000, 2_000, 3_000, 5_000, 8_000,
    15_000, 30_000, 60_000, 120_000, 300_000,
)
_HISTOGRAM_METRICS = ("queue_wait", "review_duration")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_at(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat()


def parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _duration_bucket(value_ms: int) -> int:
    value_ms = max(0, int(value_ms))
    return next(
        (upper_bound for upper_bound in _DURATION_BUCKETS_MS if value_ms <= upper_bound),
        _DURATION_BUCKETS_MS[-1],
    )


def _histogram_percentile(histogram: dict[int, int], percentile: float) -> int | None:
    samples = sum(max(0, int(count)) for count in histogram.values())
    if not samples:
        return None
    rank = max(1, int(samples * percentile + 0.999999))
    seen = 0
    for upper_bound in sorted(histogram):
        seen += max(0, int(histogram[upper_bound]))
        if seen >= rank:
            return int(upper_bound)
    return int(max(histogram))


def normalize_domain(value: object) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
        host = (parsed.hostname or "").strip(".").encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return ""
    if len(host) > 253 or "." not in host:
        return ""
    labels = host.split(".")
    if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in labels):
        return ""
    if any(not re.fullmatch(r"[a-z0-9-]+", label) for label in labels):
        return ""
    return host.removeprefix("www.")


def normalize_market(value: object) -> str:
    market = re.sub(r"\s+", " ", str(value or "").strip().lower().replace("_", " "))
    return _MARKET_ALIASES.get(market, market)


def _market_boost(country: object) -> int:
    return 5 if normalize_market(country) in _PRIORITY_MARKETS else 0


def search_priority(rank: int, country: object = "") -> int:
    return max(1, SEARCH_PRIORITY_BASE + min(max(0, rank) // 5, 12) - _market_boost(country))


def refresh_priority(state: object, country: object = "", evidence_kind: object = "") -> int:
    if str(evidence_kind or "") == "legacy_website_identity":
        base = LEGACY_RECHECK_PRIORITY
    else:
        base = {
            "recently_observed": RECENT_RECHECK_PRIORITY,
            "active_verified": ACTIVE_RECHECK_PRIORITY,
            "uncertain": UNCERTAIN_RECHECK_PRIORITY,
            "inactive": INACTIVE_RECHECK_PRIORITY,
        }.get(str(state or ""), UNCERTAIN_RECHECK_PRIORITY)
    return max(1, base - _market_boost(country))


def _legacy_evidence(reason: object, state: object = "") -> tuple[str, str]:
    mapped = {
        "website_legal_identity_match": ("official_website_legal", "strong"),
        "website_title_identity_match": ("official_website_title", "strong"),
        "website_content_identity_match": ("official_website_content", "strong"),
        "website_domain_alignment": ("official_website_domain", "moderate"),
        "website_identity_match": ("legacy_website_identity", "strong"),
    }.get(str(reason or ""))
    if mapped:
        return mapped
    if str(state or "") == "active_verified":
        return "legacy_website_identity", "strong"
    return "", ""


def _status_payload(row: sqlite3.Row | None, queue_state: str = "") -> dict[str, object]:
    if row is None:
        return {
            "vitality_state": "unchecked",
            "vitality_queue_state": queue_state,
            "vitality_confidence": 0.0,
            "vitality_checked_at": "",
            "vitality_last_public_evidence_at": "",
            "vitality_reason": "not_checked",
        }
    return {
        "vitality_state": str(row["state"] or "unchecked"),
        "vitality_queue_state": queue_state,
        "vitality_confidence": float(row["confidence"] or 0.0),
        "vitality_checked_at": str(row["checked_at"] or ""),
        "vitality_last_public_evidence_at": str(row["last_public_evidence_at"] or ""),
        "vitality_reason": str(row["reason"] or "not_checked"),
    }


class VitalityStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS company_vitality (
                    company_id TEXT PRIMARY KEY,
                    domain TEXT NOT NULL,
                    normalized_name TEXT NOT NULL DEFAULT '',
                    country TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'queued',
                    confidence REAL NOT NULL DEFAULT 0,
                    dns_status TEXT NOT NULL DEFAULT '',
                    http_status INTEGER,
                    final_url TEXT NOT NULL DEFAULT '',
                    page_title TEXT NOT NULL DEFAULT '',
                    is_parked INTEGER NOT NULL DEFAULT 0,
                    identity_score REAL NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT 'not_checked',
                    evidence_kind TEXT NOT NULL DEFAULT '',
                    evidence_strength TEXT NOT NULL DEFAULT '',
                    checked_at TEXT,
                    last_public_evidence_at TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    next_check_at TEXT,
                    updated_at TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS vitality_queue (
                    company_id TEXT PRIMARY KEY,
                    domain TEXT NOT NULL,
                    normalized_name TEXT NOT NULL DEFAULT '',
                    country TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'scheduled_refresh',
                    priority INTEGER NOT NULL DEFAULT 100,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at TEXT NOT NULL,
                    claimed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS vitality_daily_sources (
                    day TEXT NOT NULL,
                    source TEXT NOT NULL,
                    checks INTEGER NOT NULL DEFAULT 0,
                    queue_wait_ms_total INTEGER NOT NULL DEFAULT 0,
                    queue_wait_samples INTEGER NOT NULL DEFAULT 0,
                    review_duration_ms_total INTEGER NOT NULL DEFAULT 0,
                    review_duration_samples INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (day, source)
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS vitality_sample_days (
                    day TEXT PRIMARY KEY,
                    scheduled INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS vitality_daily_histograms (
                    day TEXT NOT NULL,
                    source TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    upper_bound_ms INTEGER NOT NULL,
                    samples INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (day, source, metric, upper_bound_ms)
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS vitality_sampler_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS vitality_daily_quality (
                    day TEXT PRIMARY KEY,
                    checks INTEGER NOT NULL DEFAULT 0,
                    outcome_active_verified INTEGER NOT NULL DEFAULT 0,
                    outcome_recently_observed INTEGER NOT NULL DEFAULT 0,
                    outcome_uncertain INTEGER NOT NULL DEFAULT 0,
                    outcome_inactive INTEGER NOT NULL DEFAULT 0,
                    evidence_official_website_legal INTEGER NOT NULL DEFAULT 0,
                    evidence_official_website_title INTEGER NOT NULL DEFAULT 0,
                    evidence_official_website_content INTEGER NOT NULL DEFAULT 0,
                    evidence_official_website_domain INTEGER NOT NULL DEFAULT 0,
                    evidence_legacy_website_identity INTEGER NOT NULL DEFAULT 0,
                    evidence_none INTEGER NOT NULL DEFAULT 0,
                    evidence_changes INTEGER NOT NULL DEFAULT 0,
                    visible_to_hidden INTEGER NOT NULL DEFAULT 0,
                    hidden_to_visible INTEGER NOT NULL DEFAULT 0,
                    queue_wait_ms_total INTEGER NOT NULL DEFAULT 0,
                    queue_wait_samples INTEGER NOT NULL DEFAULT 0,
                    review_duration_ms_total INTEGER NOT NULL DEFAULT 0,
                    review_duration_samples INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
            """)
            table_columns = {
                table: {
                    str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
                }
                for table in ("company_vitality", "vitality_queue")
            }
            for table, columns in {
                "company_vitality": (
                    ("country", "TEXT NOT NULL DEFAULT ''"),
                    ("evidence_kind", "TEXT NOT NULL DEFAULT ''"),
                    ("evidence_strength", "TEXT NOT NULL DEFAULT ''"),
                ),
                "vitality_queue": (
                    ("country", "TEXT NOT NULL DEFAULT ''"),
                    ("source", "TEXT NOT NULL DEFAULT 'scheduled_refresh'"),
                ),
            }.items():
                for name, definition in columns:
                    if name not in table_columns[table]:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            connection.execute(
                """UPDATE company_vitality
                SET evidence_kind='legacy_website_identity', evidence_strength='strong'
                WHERE evidence_kind='' AND state='active_verified'
                  AND reason='website_identity_match'"""
            )
            connection.execute("""
                INSERT INTO vitality_daily_sources (
                    day, source, checks, queue_wait_ms_total, queue_wait_samples,
                    review_duration_ms_total, review_duration_samples, updated_at
                )
                SELECT q.day, 'scheduled_refresh',
                    MAX(0, q.checks - COALESCE(a.checks, 0)),
                    MAX(0, q.queue_wait_ms_total - COALESCE(a.queue_wait_ms_total, 0)),
                    MAX(0, q.queue_wait_samples - COALESCE(a.queue_wait_samples, 0)),
                    MAX(0, q.review_duration_ms_total - COALESCE(a.review_duration_ms_total, 0)),
                    MAX(0, q.review_duration_samples - COALESCE(a.review_duration_samples, 0)),
                    q.updated_at
                FROM vitality_daily_quality q
                LEFT JOIN (
                    SELECT day, SUM(checks) AS checks,
                        SUM(queue_wait_ms_total) AS queue_wait_ms_total,
                        SUM(queue_wait_samples) AS queue_wait_samples,
                        SUM(review_duration_ms_total) AS review_duration_ms_total,
                        SUM(review_duration_samples) AS review_duration_samples
                    FROM vitality_daily_sources GROUP BY day
                ) a ON a.day = q.day
                WHERE q.checks > COALESCE(a.checks, 0)
                ON CONFLICT(day, source) DO UPDATE SET
                    checks = checks + excluded.checks,
                    queue_wait_ms_total = queue_wait_ms_total + excluded.queue_wait_ms_total,
                    queue_wait_samples = queue_wait_samples + excluded.queue_wait_samples,
                    review_duration_ms_total = review_duration_ms_total + excluded.review_duration_ms_total,
                    review_duration_samples = review_duration_samples + excluded.review_duration_samples,
                    updated_at = excluded.updated_at
            """)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS vitality_due_idx ON company_vitality(next_check_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS vitality_queue_ready_idx "
                "ON vitality_queue(available_at, claimed_at, priority)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS vitality_queue_source_idx "
                "ON vitality_queue(source, claimed_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS vitality_histogram_day_idx "
                "ON vitality_daily_histograms(day, source, metric)"
            )

    @staticmethod
    def _item_identity(item: dict[str, object]) -> tuple[str, str, str, str]:
        company_id = str(item.get("id") or "").strip()
        domain = normalize_domain(item.get("website_domain") or item.get("website_url") or item.get("website"))
        name = str(item.get("name_display") or item.get("name") or "").strip()
        country = normalize_market(item.get("country"))
        return company_id, domain, name, country

    def annotate_and_enqueue(self, items: list[dict[str, object]]) -> None:
        identities = [self._item_identity(item) for item in items]
        ids = [company_id for company_id, _, _, _ in identities if company_id]
        if not ids:
            for item in items:
                item.update(_status_payload(None))
            return

        now = utc_now()
        now_text = iso_at(now)
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as connection:
            existing = {
                str(row["company_id"]): row for row in connection.execute(
                    f"SELECT * FROM company_vitality WHERE company_id IN ({placeholders})", ids
                ).fetchall()
            }
            queued = {
                str(row["company_id"]): ("checking" if row["claimed_at"] else "queued")
                for row in connection.execute(
                    f"SELECT company_id, claimed_at FROM vitality_queue WHERE company_id IN ({placeholders})",
                    ids,
                ).fetchall()
            }
            queue_size = int(connection.execute("SELECT count(*) FROM vitality_queue").fetchone()[0])
            for rank, (company_id, domain, name, country) in enumerate(identities):
                if not company_id or not domain:
                    continue
                row = existing.get(company_id)
                requested_priority = search_priority(rank, country)
                if row is not None:
                    connection.execute(
                        """UPDATE company_vitality
                        SET domain=?, normalized_name=?, country=? WHERE company_id=?""",
                        (domain, name, country, company_id),
                    )
                if company_id in queued:
                    connection.execute(
                        """UPDATE vitality_queue SET domain=?, normalized_name=?, country=?,
                            source='user_search', priority=MIN(priority, ?), updated_at=?
                            WHERE company_id=?""",
                        (domain, name, country, requested_priority, now_text, company_id),
                    )
                due_at = parse_time(row["next_check_at"]) if row else None
                needs_queue = (
                    row is None
                    or (
                        row is not None
                        and row["evidence_kind"] == "legacy_website_identity"
                        and row["reason"] == "website_identity_match"
                    )
                    or (due_at is not None and due_at <= now)
                )
                if needs_queue and company_id not in queued and queue_size < MAX_QUEUE_SIZE:
                    if row is None:
                        connection.execute("""
                            INSERT OR IGNORE INTO company_vitality (
                                company_id, domain, normalized_name, country, state, reason, updated_at
                            ) VALUES (?, ?, ?, ?, 'queued', 'not_checked', ?)
                        """, (company_id, domain, name, country, now_text))
                    connection.execute("""
                        INSERT OR IGNORE INTO vitality_queue (
                            company_id, domain, normalized_name, country, source, priority,
                            available_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'user_search', ?, ?, ?, ?)
                    """, (
                        company_id, domain, name, country, requested_priority,
                        now_text, now_text, now_text,
                    ))
                    queued[company_id] = "queued"
                    queue_size += 1

            rows = {
                str(row["company_id"]): row for row in connection.execute(
                    f"SELECT * FROM company_vitality WHERE company_id IN ({placeholders})", ids
                ).fetchall()
            }

        for item, (company_id, domain, _, _) in zip(items, identities, strict=True):
            if not domain:
                item.update({
                    **_status_payload(None),
                    "vitality_state": "uncertain",
                    "vitality_reason": "missing_domain",
                })
            else:
                item.update(_status_payload(rows.get(company_id), queued.get(company_id, "")))

    def claim_next(self) -> dict[str, object] | None:
        now = utc_now()
        now_text = iso_at(now)
        stale_text = iso_at(now - CLAIM_TTL)
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("""
                SELECT q.*, v.state, v.consecutive_failures, v.last_public_evidence_at,
                    v.evidence_kind AS last_evidence_kind,
                    v.evidence_strength AS last_evidence_strength
                FROM vitality_queue q
                LEFT JOIN company_vitality v ON v.company_id = q.company_id
                WHERE q.available_at <= ? AND (q.claimed_at IS NULL OR q.claimed_at < ?)
                ORDER BY q.priority ASC, q.created_at ASC LIMIT 1
            """, (now_text, stale_text)).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute("""
                UPDATE vitality_queue SET claimed_at = ?, attempts = attempts + 1, updated_at = ?
                WHERE company_id = ?
            """, (now_text, now_text, row["company_id"]))
            if str(row["state"] or "") in {"queued", "checking", "unchecked"}:
                connection.execute(
                    "UPDATE company_vitality SET state = 'checking', updated_at = ? WHERE company_id = ?",
                    (now_text, row["company_id"]),
                )
            connection.commit()
            task = dict(row)
            created_at = parse_time(row["created_at"])
            if int(row["attempts"] or 0) == 0 and created_at is not None:
                task["queue_wait_ms"] = max(0, round((now - created_at).total_seconds() * 1000))
            return task
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def release_claims(self) -> int:
        """Release orphaned claims when the single systemd worker starts."""
        now_text = iso_at()
        with self.connect() as connection:
            released = connection.execute(
                """UPDATE vitality_queue SET claimed_at=NULL, updated_at=?
                WHERE claimed_at IS NOT NULL""",
                (now_text,),
            ).rowcount
            connection.execute(
                """UPDATE company_vitality SET state='queued', updated_at=?
                WHERE state='checking'
                  AND company_id IN (SELECT company_id FROM vitality_queue)""",
                (now_text,),
            )
        return max(0, released)

    def complete(self, task: dict[str, object], observation: dict[str, object]) -> None:
        company_id = str(task["company_id"])
        now = utc_now()
        checked_at = str(observation.get("checked_at") or iso_at(now))
        observed_state = str(observation.get("state") or "uncertain")
        reason = str(observation.get("reason") or "worker_error")
        previous_evidence = str(task.get("last_public_evidence_at") or "")
        previous_failures = int(task.get("consecutive_failures") or 0)
        previous_kind = str(task.get("last_evidence_kind") or "")
        previous_strength = str(task.get("last_evidence_strength") or "")
        observed_kind = str(observation.get("evidence_kind") or "")
        observed_strength = str(observation.get("evidence_strength") or "")
        if not observed_kind:
            observed_kind, observed_strength = _legacy_evidence(reason, observed_state)

        if observed_state == "active_verified":
            state = observed_state
            confidence = float(observation.get("confidence") or 0.0)
            failures = 0
            evidence_at = checked_at
            evidence_kind = observed_kind
            evidence_strength = observed_strength or "strong"
            next_check = now + ACTIVE_TTL
        elif observed_state == "recently_observed":
            state = observed_state
            confidence = float(observation.get("confidence") or 0.0)
            failures = 0
            evidence_at = checked_at
            evidence_kind = observed_kind
            evidence_strength = observed_strength or "moderate"
            next_check = now + MODERATE_TTL
        elif reason == "nxdomain" and previous_failures == 0:
            # Require a second observation before hiding a company so a DNS
            # resolver incident cannot invalidate a large candidate set.
            state = "uncertain"
            confidence = 0.55
            failures = 1
            evidence_at = previous_evidence
            evidence_kind = previous_kind
            evidence_strength = previous_strength
            next_check = now + RETRY_TTL
        elif reason == "website_identity_uncertain" and previous_evidence and previous_failures == 0:
            # A classifier upgrade must not remove an existing public company
            # on its first weaker observation. Recheck once on the next day.
            state = "recently_observed"
            confidence = 0.5
            failures = 1
            evidence_at = previous_evidence
            evidence_kind = previous_kind
            evidence_strength = previous_strength
            next_check = now + RETRY_TTL
        elif reason in _STRONG_INACTIVE_REASONS:
            state = "inactive"
            confidence = float(observation.get("confidence") or 0.9)
            failures = previous_failures + 1
            evidence_at = previous_evidence
            evidence_kind = previous_kind
            evidence_strength = previous_strength
            next_check = now + INACTIVE_TTL
        else:
            failures = previous_failures + 1
            state = "recently_observed" if previous_evidence and reason in _TRANSIENT_REASONS else "uncertain"
            confidence = 0.5 if state == "recently_observed" else float(observation.get("confidence") or 0.25)
            evidence_at = previous_evidence
            evidence_kind = previous_kind
            evidence_strength = previous_strength
            next_check = now + (RETRY_TTL if state == "recently_observed" else UNCERTAIN_TTL)

        previous_state = str(task.get("state") or "unchecked")
        previous_visible = previous_state in _PUBLIC_STATES
        current_visible = state in _PUBLIC_STATES
        outcome_values = {key: int(state == key) for key in _QUALITY_STATES}
        evidence_bucket = evidence_kind if evidence_kind in _QUALITY_EVIDENCE else "none"
        evidence_values = {key: int(evidence_bucket == key) for key in _QUALITY_EVIDENCE}
        queue_wait_ms = max(0, int(task.get("queue_wait_ms") or 0))
        review_duration_ms = max(0, int(observation.get("review_duration_ms") or 0))
        source = str(task.get("source") or "scheduled_refresh")
        if source not in _QUALITY_SOURCES:
            source = "scheduled_refresh"
        report_day = (parse_time(checked_at) or now).astimezone(timezone.utc).date().isoformat()

        with self.connect() as connection:
            connection.execute("""
                INSERT INTO company_vitality (
                    company_id, domain, normalized_name, country, state, confidence, dns_status,
                    http_status, final_url, page_title, is_parked, identity_score, reason,
                    evidence_kind, evidence_strength, checked_at, last_public_evidence_at,
                    consecutive_failures, next_check_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(company_id) DO UPDATE SET
                    domain = excluded.domain,
                    normalized_name = excluded.normalized_name,
                    country = excluded.country,
                    state = excluded.state,
                    confidence = excluded.confidence,
                    dns_status = excluded.dns_status,
                    http_status = excluded.http_status,
                    final_url = excluded.final_url,
                    page_title = excluded.page_title,
                    is_parked = excluded.is_parked,
                    identity_score = excluded.identity_score,
                    reason = excluded.reason,
                    evidence_kind = excluded.evidence_kind,
                    evidence_strength = excluded.evidence_strength,
                    checked_at = excluded.checked_at,
                    last_public_evidence_at = excluded.last_public_evidence_at,
                    consecutive_failures = excluded.consecutive_failures,
                    next_check_at = excluded.next_check_at,
                    updated_at = excluded.updated_at
            """, (
                company_id, str(task["domain"]), str(task.get("normalized_name") or ""),
                normalize_market(task.get("country")), state, confidence,
                str(observation.get("dns_status") or ""),
                observation.get("http_status"), str(observation.get("final_url") or ""),
                str(observation.get("page_title") or "")[:500], int(bool(observation.get("is_parked"))),
                float(observation.get("identity_score") or 0.0), reason,
                evidence_kind, evidence_strength, checked_at, evidence_at or None,
                failures, iso_at(next_check), iso_at(now),
            ))
            connection.execute("DELETE FROM vitality_queue WHERE company_id = ?", (company_id,))
            connection.execute("""
                INSERT INTO vitality_daily_quality (
                    day, checks,
                    outcome_active_verified, outcome_recently_observed,
                    outcome_uncertain, outcome_inactive,
                    evidence_official_website_legal, evidence_official_website_title,
                    evidence_official_website_content, evidence_official_website_domain,
                    evidence_legacy_website_identity, evidence_none, evidence_changes,
                    visible_to_hidden, hidden_to_visible,
                    queue_wait_ms_total, queue_wait_samples,
                    review_duration_ms_total, review_duration_samples, updated_at
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(day) DO UPDATE SET
                    checks = checks + excluded.checks,
                    outcome_active_verified = outcome_active_verified + excluded.outcome_active_verified,
                    outcome_recently_observed = outcome_recently_observed + excluded.outcome_recently_observed,
                    outcome_uncertain = outcome_uncertain + excluded.outcome_uncertain,
                    outcome_inactive = outcome_inactive + excluded.outcome_inactive,
                    evidence_official_website_legal = evidence_official_website_legal + excluded.evidence_official_website_legal,
                    evidence_official_website_title = evidence_official_website_title + excluded.evidence_official_website_title,
                    evidence_official_website_content = evidence_official_website_content + excluded.evidence_official_website_content,
                    evidence_official_website_domain = evidence_official_website_domain + excluded.evidence_official_website_domain,
                    evidence_legacy_website_identity = evidence_legacy_website_identity + excluded.evidence_legacy_website_identity,
                    evidence_none = evidence_none + excluded.evidence_none,
                    evidence_changes = evidence_changes + excluded.evidence_changes,
                    visible_to_hidden = visible_to_hidden + excluded.visible_to_hidden,
                    hidden_to_visible = hidden_to_visible + excluded.hidden_to_visible,
                    queue_wait_ms_total = queue_wait_ms_total + excluded.queue_wait_ms_total,
                    queue_wait_samples = queue_wait_samples + excluded.queue_wait_samples,
                    review_duration_ms_total = review_duration_ms_total + excluded.review_duration_ms_total,
                    review_duration_samples = review_duration_samples + excluded.review_duration_samples,
                    updated_at = excluded.updated_at
            """, (
                report_day,
                *(outcome_values[key] for key in _QUALITY_STATES),
                *(evidence_values[key] for key in _QUALITY_EVIDENCE),
                int(previous_kind != evidence_kind),
                int(previous_visible and not current_visible),
                int(not previous_visible and current_visible),
                queue_wait_ms, int("queue_wait_ms" in task),
                review_duration_ms, int("review_duration_ms" in observation), iso_at(now),
            ))
            connection.execute("""
                INSERT INTO vitality_daily_sources (
                    day, source, checks, queue_wait_ms_total, queue_wait_samples,
                    review_duration_ms_total, review_duration_samples, updated_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(day, source) DO UPDATE SET
                    checks = checks + excluded.checks,
                    queue_wait_ms_total = queue_wait_ms_total + excluded.queue_wait_ms_total,
                    queue_wait_samples = queue_wait_samples + excluded.queue_wait_samples,
                    review_duration_ms_total = review_duration_ms_total + excluded.review_duration_ms_total,
                    review_duration_samples = review_duration_samples + excluded.review_duration_samples,
                    updated_at = excluded.updated_at
            """, (
                report_day, source, queue_wait_ms, int("queue_wait_ms" in task),
                review_duration_ms, int("review_duration_ms" in observation), iso_at(now),
            ))
            histogram_rows = []
            if "queue_wait_ms" in task:
                histogram_rows.append((
                    report_day, source, "queue_wait", _duration_bucket(queue_wait_ms), iso_at(now),
                ))
            if "review_duration_ms" in observation:
                histogram_rows.append((
                    report_day, source, "review_duration",
                    _duration_bucket(review_duration_ms), iso_at(now),
                ))
            if histogram_rows:
                connection.executemany("""
                    INSERT INTO vitality_daily_histograms (
                        day, source, metric, upper_bound_ms, samples, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?)
                    ON CONFLICT(day, source, metric, upper_bound_ms) DO UPDATE SET
                        samples = samples + excluded.samples,
                        updated_at = excluded.updated_at
                """, histogram_rows)

    def enqueue_due(self, limit: int = 100) -> int:
        now_text = iso_at()
        inserted = 0
        with self.connect() as connection:
            queue_size = int(connection.execute("SELECT count(*) FROM vitality_queue").fetchone()[0])
            remaining = max(0, min(limit, MAX_QUEUE_SIZE - queue_size))
            if not remaining:
                return 0
            rows = connection.execute("""
                SELECT company_id, domain, normalized_name, country, state, evidence_kind
                FROM company_vitality
                WHERE ((evidence_kind = 'legacy_website_identity'
                        AND reason = 'website_identity_match')
                       OR (next_check_at IS NOT NULL AND next_check_at <= ?))
                  AND company_id NOT IN (SELECT company_id FROM vitality_queue)
                ORDER BY CASE
                    WHEN evidence_kind = 'legacy_website_identity' THEN 1
                    WHEN state = 'recently_observed' THEN 2
                    WHEN state = 'active_verified' THEN 3
                    WHEN state = 'uncertain' THEN 4
                    ELSE 5 END,
                    next_check_at ASC
                LIMIT ?
            """, (now_text, remaining)).fetchall()
            for row in rows:
                connection.execute("""
                    INSERT OR IGNORE INTO vitality_queue (
                        company_id, domain, normalized_name, country, source, priority,
                        available_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'scheduled_refresh', ?, ?, ?, ?)
                """, (
                    row["company_id"], row["domain"], row["normalized_name"], row["country"],
                    refresh_priority(row["state"], row["country"], row["evidence_kind"]),
                    now_text, now_text, now_text,
                ))
                inserted += 1
        return inserted

    def sampler_started_at(self) -> datetime:
        now = utc_now()
        now_text = iso_at(now)
        with self.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO vitality_sampler_meta (key, value, updated_at)
                VALUES ('started_at', ?, ?)""",
                (now_text, now_text),
            )
            value = connection.execute(
                "SELECT value FROM vitality_sampler_meta WHERE key='started_at'"
            ).fetchone()[0]
        return parse_time(value) or now

    def sample_day_scheduled(self, day: str | None = None) -> int:
        sample_day = day or utc_now().date().isoformat()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT scheduled FROM vitality_sample_days WHERE day=?", (sample_day,)
            ).fetchone()
        return int(row[0]) if row else 0

    def record_sampler_run(self, target: int, status: str, mode: str) -> None:
        now_text = iso_at()
        values = {
            "target_per_day": str(max(0, int(target))),
            "last_status": str(status or "unknown")[:40],
            "mode": str(mode or "unknown")[:20],
            "last_run_at": now_text,
        }
        with self.connect() as connection:
            connection.executemany("""
                INSERT INTO vitality_sampler_meta (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """, [(key, value, now_text) for key, value in values.items()])

    def sampler_status(self) -> dict[str, object]:
        with self.connect() as connection:
            values = {
                str(row["key"]): str(row["value"])
                for row in connection.execute(
                    "SELECT key, value FROM vitality_sampler_meta"
                ).fetchall()
            }
        return {
            "started_at": values.get("started_at", ""),
            "last_run_at": values.get("last_run_at", ""),
            "last_status": values.get("last_status", "not_started"),
            "mode": values.get("mode", "not_started"),
            "target_per_day": int(values.get("target_per_day") or 0),
            "scheduled_today": self.sample_day_scheduled(),
        }

    def sample_cohort(
        self,
        states: tuple[str, ...],
        limit: int,
        minimum_age: timedelta,
        *,
        include_legacy: bool = False,
    ) -> list[dict[str, object]]:
        states = tuple(state for state in states if state in _QUALITY_STATES)
        if not states or limit <= 0:
            return []
        placeholders = ",".join("?" for _ in states)
        cutoff = iso_at(utc_now() - minimum_age)
        legacy_clause = " OR evidence_kind='legacy_website_identity'" if include_legacy else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT company_id, domain, normalized_name, country
                FROM company_vitality
                WHERE ((state IN ({placeholders})
                        AND checked_at IS NOT NULL AND checked_at <= ?){legacy_clause})
                  AND company_id NOT IN (SELECT company_id FROM vitality_queue)
                ORDER BY checked_at ASC LIMIT ?""",
                (*states, cutoff, limit),
            ).fetchall()
        return [{
            "id": str(row["company_id"]),
            "website_domain": str(row["domain"]),
            "name_display": str(row["normalized_name"]),
            "country": str(row["country"]),
            "_sample_existing": True,
        } for row in rows]

    def enqueue_samples(
        self,
        items: list[dict[str, object]],
        *,
        daily_limit: int,
        max_batch: int,
        queue_limit: int = 500,
    ) -> dict[str, int | str]:
        now = utc_now()
        now_text = iso_at(now)
        day = now.date().isoformat()
        identities: list[tuple[str, str, str, str, bool]] = []
        seen: set[str] = set()
        for item in items:
            company_id, domain, name, country = self._item_identity(item)
            if not company_id or not domain or company_id in seen:
                continue
            seen.add(company_id)
            identities.append((company_id, domain, name, country, bool(item.get("_sample_existing"))))

        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            queue_size = int(connection.execute("SELECT count(*) FROM vitality_queue").fetchone()[0])
            sample_queued = int(connection.execute(
                "SELECT count(*) FROM vitality_queue WHERE source='daily_sample'"
            ).fetchone()[0])
            row = connection.execute(
                "SELECT scheduled FROM vitality_sample_days WHERE day=?", (day,)
            ).fetchone()
            scheduled = int(row[0]) if row else 0
            allowance = min(
                max(0, max_batch), max(0, daily_limit - scheduled),
                max(0, queue_limit - queue_size), max(0, MAX_QUEUE_SIZE - queue_size),
            )
            if not allowance:
                connection.commit()
                return {
                    "inserted": 0, "scheduled": scheduled, "queued": queue_size,
                    "sample_queued": sample_queued, "reason": "limit_reached",
                }

            ids = [identity[0] for identity in identities]
            existing: set[str] = set()
            queued: set[str] = set()
            if ids:
                placeholders = ",".join("?" for _ in ids)
                existing = {
                    str(record[0]) for record in connection.execute(
                        f"SELECT company_id FROM company_vitality WHERE company_id IN ({placeholders})",
                        ids,
                    ).fetchall()
                }
                queued = {
                    str(record[0]) for record in connection.execute(
                        f"SELECT company_id FROM vitality_queue WHERE company_id IN ({placeholders})",
                        ids,
                    ).fetchall()
                }

            inserted = 0
            for company_id, domain, name, country, allow_existing in identities:
                if inserted >= allowance or company_id in queued:
                    continue
                if company_id in existing and not allow_existing:
                    continue
                if company_id in existing:
                    connection.execute(
                        """UPDATE company_vitality SET domain=?, normalized_name=?, country=?
                        WHERE company_id=?""",
                        (domain, name, country, company_id),
                    )
                else:
                    connection.execute("""
                        INSERT INTO company_vitality (
                            company_id, domain, normalized_name, country, state, reason, updated_at
                        ) VALUES (?, ?, ?, ?, 'queued', 'not_checked', ?)
                    """, (company_id, domain, name, country, now_text))
                connection.execute("""
                    INSERT INTO vitality_queue (
                        company_id, domain, normalized_name, country, source, priority,
                        available_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'daily_sample', ?, ?, ?, ?)
                """, (
                    company_id, domain, name, country, SAMPLE_PRIORITY,
                    now_text, now_text, now_text,
                ))
                inserted += 1
                queued.add(company_id)

            if inserted:
                connection.execute("""
                    INSERT INTO vitality_sample_days (day, scheduled, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(day) DO UPDATE SET
                        scheduled = scheduled + excluded.scheduled,
                        updated_at = excluded.updated_at
                """, (day, inserted, now_text))
            connection.commit()
            return {
                "inserted": inserted, "scheduled": scheduled + inserted,
                "queued": queue_size + inserted, "sample_queued": sample_queued + inserted,
                "reason": "scheduled" if inserted else "no_candidates",
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def stats(self) -> dict[str, object]:
        with self.connect() as connection:
            states = {
                str(row["state"]): int(row["count"])
                for row in connection.execute(
                    "SELECT state, count(*) AS count FROM company_vitality GROUP BY state"
                ).fetchall()
            }
            queued = int(connection.execute("SELECT count(*) FROM vitality_queue").fetchone()[0])
            checking = int(connection.execute(
                "SELECT count(*) FROM vitality_queue WHERE claimed_at IS NOT NULL"
            ).fetchone()[0])
            evidence = {
                str(row["evidence_kind"] or "none"): int(row["count"])
                for row in connection.execute(
                    "SELECT evidence_kind, count(*) AS count FROM company_vitality "
                    "GROUP BY evidence_kind"
                ).fetchall()
            }
            priorities = {
                str(row["priority"]): int(row["count"])
                for row in connection.execute(
                    "SELECT priority, count(*) AS count FROM vitality_queue GROUP BY priority"
                ).fetchall()
            }
            sources = {
                str(row["source"]): int(row["count"])
                for row in connection.execute(
                    "SELECT source, count(*) AS count FROM vitality_queue GROUP BY source"
                ).fetchall()
            }
        return {
            "enabled": True, "states": states, "queued": queued, "checking": checking,
            "evidence": evidence, "queue_priorities": priorities, "queue_sources": sources,
        }

    @staticmethod
    def _duration_quality(
        row: dict[str, object], metric: str, histogram: dict[int, int] | None = None,
    ) -> dict[str, object]:
        sample_key = "queue_wait_samples" if metric == "queue_wait" else "review_duration_samples"
        total_key = "queue_wait_ms_total" if metric == "queue_wait" else "review_duration_ms_total"
        samples = int(row.get(sample_key) or 0)
        histogram = histogram or {}
        percentile_samples = sum(max(0, int(count)) for count in histogram.values())
        return {
            "average_ms": round(int(row.get(total_key) or 0) / samples) if samples else 0,
            "p95_ms": _histogram_percentile(histogram, 0.95),
            "p99_ms": _histogram_percentile(histogram, 0.99),
            "percentile_samples": percentile_samples,
            "samples": samples,
        }

    @classmethod
    def _source_quality_row(
        cls, row: dict[str, object], histograms: dict[str, dict[int, int]] | None = None,
    ) -> dict[str, object]:
        histograms = histograms or {}
        return {
            "checks": int(row.get("checks") or 0),
            "queue_wait": cls._duration_quality(
                row, "queue_wait", histograms.get("queue_wait"),
            ),
            "review_duration": cls._duration_quality(
                row, "review_duration", histograms.get("review_duration"),
            ),
        }

    @classmethod
    def _quality_row(
        cls, row: dict[str, object], histograms: dict[str, dict[int, int]] | None = None,
    ) -> dict[str, object]:
        hidden_to_visible = int(row.get("hidden_to_visible") or 0)
        visible_to_hidden = int(row.get("visible_to_hidden") or 0)
        histograms = histograms or {}
        return {
            "day": str(row.get("day") or ""),
            "checks": int(row.get("checks") or 0),
            "outcomes": {
                key: int(row.get(f"outcome_{key}") or 0) for key in _QUALITY_STATES
            },
            "evidence": {
                key: int(row.get(f"evidence_{key}") or 0) for key in _QUALITY_EVIDENCE
            },
            "evidence_changes": int(row.get("evidence_changes") or 0),
            "transitions": {
                "visible_to_hidden": visible_to_hidden,
                "hidden_to_visible": hidden_to_visible,
                "net_public": hidden_to_visible - visible_to_hidden,
            },
            "queue_wait": cls._duration_quality(
                row, "queue_wait", histograms.get("queue_wait"),
            ),
            "review_duration": cls._duration_quality(
                row, "review_duration", histograms.get("review_duration"),
            ),
        }

    def report(self, days: int = 14) -> dict[str, object]:
        days = max(1, min(90, int(days)))
        today = utc_now().date()
        first_day = today - timedelta(days=days - 1)
        with self.connect() as connection:
            rows = {
                str(row["day"]): dict(row)
                for row in connection.execute(
                    "SELECT * FROM vitality_daily_quality WHERE day >= ? ORDER BY day",
                    (first_day.isoformat(),),
                ).fetchall()
            }
            source_rows = {
                (str(row["day"]), str(row["source"])): dict(row)
                for row in connection.execute(
                    "SELECT * FROM vitality_daily_sources WHERE day >= ? ORDER BY day, source",
                    (first_day.isoformat(),),
                ).fetchall()
            }
            histogram_rows = connection.execute(
                """SELECT day, source, metric, upper_bound_ms, samples
                FROM vitality_daily_histograms WHERE day >= ?
                ORDER BY day, source, metric, upper_bound_ms""",
                (first_day.isoformat(),),
            ).fetchall()
        histograms: dict[tuple[str, str, str], dict[int, int]] = {}
        for row in histogram_rows:
            key = (str(row["day"]), str(row["source"]), str(row["metric"]))
            histograms.setdefault(key, {})[int(row["upper_bound_ms"])] = int(row["samples"])

        def histogram_for(day: str, source: str) -> dict[str, dict[int, int]]:
            return {
                metric: histograms.get((day, source, metric), {})
                for metric in _HISTOGRAM_METRICS
            }

        def merge_histograms(
            target: dict[str, dict[int, int]], addition: dict[str, dict[int, int]],
        ) -> None:
            for metric in _HISTOGRAM_METRICS:
                for upper_bound, count in addition.get(metric, {}).items():
                    metric_target = target.setdefault(metric, {})
                    metric_target[upper_bound] = metric_target.get(upper_bound, 0) + count
        daily: list[dict[str, object]] = []
        totals: dict[str, object] = {
            "day": "", "checks": 0,
            **{f"outcome_{key}": 0 for key in _QUALITY_STATES},
            **{f"evidence_{key}": 0 for key in _QUALITY_EVIDENCE},
            "evidence_changes": 0, "visible_to_hidden": 0, "hidden_to_visible": 0,
            "queue_wait_ms_total": 0, "queue_wait_samples": 0,
            "review_duration_ms_total": 0, "review_duration_samples": 0,
        }
        numeric_keys = tuple(key for key in totals if key != "day")
        source_totals = {
            source: {
                "checks": 0, "queue_wait_ms_total": 0, "queue_wait_samples": 0,
                "review_duration_ms_total": 0, "review_duration_samples": 0,
            }
            for source in _QUALITY_SOURCES
        }
        source_numeric_keys = tuple(next(iter(source_totals.values())))
        total_histograms = {metric: {} for metric in _HISTOGRAM_METRICS}
        source_histogram_totals = {
            source: {metric: {} for metric in _HISTOGRAM_METRICS}
            for source in _QUALITY_SOURCES
        }
        for offset in range(days):
            day = (first_day + timedelta(days=offset)).isoformat()
            raw = {"day": day, **rows.get(day, {})}
            daily_histograms = {metric: {} for metric in _HISTOGRAM_METRICS}
            for source in _QUALITY_SOURCES:
                merge_histograms(daily_histograms, histogram_for(day, source))
            daily_row = self._quality_row(raw, daily_histograms)
            daily_row["sources"] = {}
            for source in _QUALITY_SOURCES:
                source_raw = source_rows.get((day, source), {})
                source_histograms = histogram_for(day, source)
                daily_row["sources"][source] = self._source_quality_row(
                    source_raw, source_histograms,
                )
                for key in source_numeric_keys:
                    source_totals[source][key] += int(source_raw.get(key) or 0)
                merge_histograms(source_histogram_totals[source], source_histograms)
            daily.append(daily_row)
            for key in numeric_keys:
                totals[key] = int(totals[key]) + int(raw.get(key) or 0)
            merge_histograms(total_histograms, daily_histograms)
        total_payload = self._quality_row(totals, total_histograms)
        total_payload["sources"] = {
            source: self._source_quality_row(
                source_totals[source], source_histogram_totals[source],
            )
            for source in _QUALITY_SOURCES
        }
        return {
            "days": days,
            "generated_at": iso_at(),
            "daily": daily,
            "totals": total_payload,
            "current": self.stats(),
            "sampler": self.sampler_status(),
        }

    def get(self, company_id: str) -> dict[str, object] | None:
        """Return the latest public vitality observation for one company."""
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT v.*, q.company_id AS queue_company_id, q.claimed_at AS queue_claimed_at
                FROM company_vitality v
                LEFT JOIN vitality_queue q ON q.company_id = v.company_id
                WHERE v.company_id = ?
                """,
                (company_id,),
            ).fetchone()
        if row is None:
            return None
        queue_state = ""
        if row["queue_company_id"]:
            queue_state = "checking" if row["queue_claimed_at"] else "queued"
        payload = _status_payload(row, queue_state)
        payload.update({
            "dns_status": str(row["dns_status"] or ""),
            "http_status": row["http_status"],
            "final_url": str(row["final_url"] or ""),
            "page_title": str(row["page_title"] or ""),
            "is_parked": bool(row["is_parked"]),
            "identity_score": float(row["identity_score"] or 0.0),
            "evidence_kind": str(row["evidence_kind"] or ""),
            "evidence_strength": str(row["evidence_strength"] or ""),
            "country": str(row["country"] or ""),
        })
        return payload


def _resolve_public(domain: str) -> tuple[list[str], str]:
    try:
        addresses = sorted({
            item[4][0] for item in socket.getaddrinfo(domain, 443, type=socket.SOCK_STREAM)
        })
    except socket.gaierror as exc:
        reason = "nxdomain" if exc.errno == socket.EAI_NONAME else "dns_temporary_failure"
        return [], reason
    except OSError:
        return [], "dns_temporary_failure"
    if not addresses:
        return [], "nxdomain"
    try:
        if any(not ipaddress.ip_address(address).is_global for address in addresses):
            return [], "non_public_address"
    except ValueError:
        return [], "dns_temporary_failure"
    return addresses, "ok"


class PublicOnlyRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Request:
        host = normalize_domain(newurl)
        if not host:
            raise URLError("invalid redirect domain")
        _, status = _resolve_public(host)
        if status != "ok":
            raise URLError("redirect target is not public")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _page_text(body: bytes, content_type: str) -> tuple[str, str]:
    charset = "utf-8"
    match = re.search(r"charset=([a-zA-Z0-9._-]+)", content_type or "", re.IGNORECASE)
    if match:
        charset = match.group(1)
    try:
        source = body.decode(charset, errors="replace")
    except LookupError:
        source = body.decode("utf-8", errors="replace")
    title_match = _TITLE_RE.search(source)
    title = html.unescape(_TAG_RE.sub(" ", title_match.group(1))).strip() if title_match else ""
    visible = _TAG_RE.sub(" ", _NOISE_RE.sub(" ", source))
    visible = re.sub(r"\s+", " ", html.unescape(visible)).strip().lower()
    return title[:500], visible


def _identity_match_score(tokens: list[str], text: str) -> float:
    if not tokens or not text:
        return 0.0
    matches = sum(1 for token in tokens if token in text)
    score = matches / len(tokens)
    compact_name = "".join(tokens)
    compact_text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", text[:30_000])
    if compact_name and len(compact_name) >= 4 and compact_name in compact_text:
        return 1.0
    return score


def _legal_evidence_market(country: object, text: str) -> str:
    market = normalize_market(country)
    patterns = _MARKET_LEGAL_PATTERNS.get(market, ())
    return market if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns) else ""


def classify_page(
    company_name: str, domain: str, body: bytes, content_type: str = "", country: str = "",
) -> dict[str, object]:
    title, visible = _page_text(body, content_type)
    evidence_text = f"{title.lower()} {visible[:120_000]}"
    if any(phrase in evidence_text for phrase in _PARKING_PHRASES):
        return {
            "state": "inactive", "confidence": 0.97, "reason": "parked_domain",
            "is_parked": True, "identity_score": 0.0, "page_title": title,
        }

    name_tokens = [
        token for token in _TOKEN_RE.findall(company_name.lower())
        if len(token) > 1 and token not in _COMMON_COMPANY_TOKENS
    ]
    unique_tokens = list(dict.fromkeys(name_tokens))
    title_score = _identity_match_score(unique_tokens, title.lower())
    body_text = visible
    if title:
        body_text = body_text.replace(title.lower(), " ", 1)
    body_score = _identity_match_score(unique_tokens, body_text)
    identity_score = max(title_score, body_score)
    domain_token = domain.split(".", 1)[0].replace("-", "").replace("_", "")
    compact_tokens = "".join(unique_tokens)
    domain_aligned = bool(
        domain_token and compact_tokens and len(domain_token) >= 4
        and (domain_token in compact_tokens or compact_tokens in domain_token)
    )
    legal_market = _legal_evidence_market(country, evidence_text)

    if legal_market and identity_score >= 0.45:
        return {
            "state": "active_verified", "confidence": min(0.99, 0.82 + identity_score * 0.17),
            "reason": "website_legal_identity_match", "evidence_kind": "official_website_legal",
            "evidence_strength": "strong", "evidence_market": legal_market, "is_parked": False,
            "identity_score": identity_score, "page_title": title,
        }
    if title_score >= 0.45:
        return {
            "state": "active_verified", "confidence": min(0.98, 0.76 + title_score * 0.22),
            "reason": "website_title_identity_match", "evidence_kind": "official_website_title",
            "evidence_strength": "strong", "is_parked": False,
            "identity_score": identity_score, "page_title": title,
        }
    if body_score >= 0.45:
        return {
            "state": "active_verified", "confidence": min(0.96, 0.7 + body_score * 0.24),
            "reason": "website_content_identity_match", "evidence_kind": "official_website_content",
            "evidence_strength": "strong", "is_parked": False,
            "identity_score": identity_score, "page_title": title,
        }
    if domain_aligned:
        return {
            "state": "recently_observed", "confidence": 0.64,
            "reason": "website_domain_alignment", "evidence_kind": "official_website_domain",
            "evidence_strength": "moderate", "is_parked": False,
            "identity_score": max(identity_score, 0.4), "page_title": title,
        }
    return {
        "state": "uncertain", "confidence": 0.35, "reason": "website_identity_uncertain",
        "is_parked": False, "identity_score": identity_score, "page_title": title,
    }


def probe_company(task: dict[str, object], timeout: float = 5.0) -> dict[str, object]:
    checked_at = iso_at()
    domain = normalize_domain(task.get("domain"))
    if not domain:
        return {
            "state": "inactive", "confidence": 0.95, "reason": "invalid_domain",
            "dns_status": "invalid", "checked_at": checked_at,
        }
    _, dns_status = _resolve_public(domain)
    if dns_status != "ok":
        return {
            "state": "inactive" if dns_status in _STRONG_INACTIVE_REASONS else "uncertain",
            "confidence": 0.95 if dns_status in _STRONG_INACTIVE_REASONS else 0.2,
            "reason": dns_status, "dns_status": dns_status, "checked_at": checked_at,
        }

    opener = build_opener(PublicOnlyRedirectHandler())
    last_reason = "connection_failed"
    for scheme in ("https", "http"):
        url = f"{scheme}://{domain}/"
        request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
        try:
            response = opener.open(request, timeout=timeout)
            status = int(response.status)
            final_url = response.geturl()
            content_type = response.headers.get("Content-Type", "")
            body = response.read(MAX_RESPONSE_BYTES)
        except HTTPError as exc:
            status = int(exc.code)
            final_url = exc.geturl()
            content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
            body = exc.read(MAX_RESPONSE_BYTES) if status in {401, 403, 404} else b""
        except ssl.SSLError:
            last_reason = "tls_failure"
            continue
        except (TimeoutError, URLError, OSError):
            last_reason = "connection_failed"
            continue

        if status >= 500:
            last_reason = "http_5xx"
            continue
        if status in {401, 403}:
            return {
                "state": "uncertain", "confidence": 0.35, "reason": "http_restricted",
                "dns_status": "ok", "http_status": status, "final_url": final_url,
                "checked_at": checked_at,
            }
        if status >= 400:
            return {
                "state": "uncertain", "confidence": 0.3, "reason": "http_4xx",
                "dns_status": "ok", "http_status": status, "final_url": final_url,
                "checked_at": checked_at,
            }

        result = classify_page(
            str(task.get("normalized_name") or ""), domain, body, content_type,
            normalize_market(task.get("country")),
        )
        result.update({
            "dns_status": "ok", "http_status": status, "final_url": final_url,
            "checked_at": checked_at,
        })
        return result

    return {
        "state": "uncertain", "confidence": 0.2, "reason": last_reason,
        "dns_status": "ok", "checked_at": checked_at,
    }
