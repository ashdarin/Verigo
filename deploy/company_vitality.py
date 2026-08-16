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
RECENT_TTL = timedelta(days=1)
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
    "and", "company", "co", "corporation", "corp", "group", "holding", "holdings",
    "inc", "international", "limited", "llc", "ltd", "plc", "the",
})


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
                    state TEXT NOT NULL DEFAULT 'queued',
                    confidence REAL NOT NULL DEFAULT 0,
                    dns_status TEXT NOT NULL DEFAULT '',
                    http_status INTEGER,
                    final_url TEXT NOT NULL DEFAULT '',
                    page_title TEXT NOT NULL DEFAULT '',
                    is_parked INTEGER NOT NULL DEFAULT 0,
                    identity_score REAL NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT 'not_checked',
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
                    priority INTEGER NOT NULL DEFAULT 100,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at TEXT NOT NULL,
                    claimed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS vitality_due_idx ON company_vitality(next_check_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS vitality_queue_ready_idx "
                "ON vitality_queue(available_at, claimed_at, priority)"
            )

    @staticmethod
    def _item_identity(item: dict[str, object]) -> tuple[str, str, str]:
        company_id = str(item.get("id") or "").strip()
        domain = normalize_domain(item.get("website_domain") or item.get("website_url") or item.get("website"))
        name = str(item.get("name_display") or item.get("name") or "").strip()
        return company_id, domain, name

    def annotate_and_enqueue(self, items: list[dict[str, object]]) -> None:
        identities = [self._item_identity(item) for item in items]
        ids = [company_id for company_id, _, _ in identities if company_id]
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
            for company_id, domain, name in identities:
                if not company_id or not domain:
                    continue
                row = existing.get(company_id)
                due_at = parse_time(row["next_check_at"]) if row else None
                needs_queue = row is None or (due_at is not None and due_at <= now)
                if needs_queue and company_id not in queued and queue_size < MAX_QUEUE_SIZE:
                    if row is None:
                        connection.execute("""
                            INSERT OR IGNORE INTO company_vitality (
                                company_id, domain, normalized_name, state, reason, updated_at
                            ) VALUES (?, ?, ?, 'queued', 'not_checked', ?)
                        """, (company_id, domain, name, now_text))
                    connection.execute("""
                        INSERT OR IGNORE INTO vitality_queue (
                            company_id, domain, normalized_name, available_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                    """, (company_id, domain, name, now_text, now_text, now_text))
                    queued[company_id] = "queued"
                    queue_size += 1

            rows = {
                str(row["company_id"]): row for row in connection.execute(
                    f"SELECT * FROM company_vitality WHERE company_id IN ({placeholders})", ids
                ).fetchall()
            }

        for item, (company_id, domain, _) in zip(items, identities, strict=True):
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
                SELECT q.*, v.state, v.consecutive_failures, v.last_public_evidence_at
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
            return dict(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def complete(self, task: dict[str, object], observation: dict[str, object]) -> None:
        company_id = str(task["company_id"])
        now = utc_now()
        checked_at = str(observation.get("checked_at") or iso_at(now))
        observed_state = str(observation.get("state") or "uncertain")
        reason = str(observation.get("reason") or "worker_error")
        previous_evidence = str(task.get("last_public_evidence_at") or "")
        previous_failures = int(task.get("consecutive_failures") or 0)

        if observed_state == "active_verified":
            state = observed_state
            confidence = float(observation.get("confidence") or 0.0)
            failures = 0
            evidence_at = checked_at
            next_check = now + ACTIVE_TTL
        elif observed_state == "recently_observed":
            state = observed_state
            confidence = float(observation.get("confidence") or 0.0)
            failures = 0
            evidence_at = checked_at
            next_check = now + RECENT_TTL
        elif reason == "nxdomain" and previous_failures == 0:
            # Require a second observation before hiding a company so a DNS
            # resolver incident cannot invalidate a large candidate set.
            state = "uncertain"
            confidence = 0.55
            failures = 1
            evidence_at = previous_evidence
            next_check = now + RECENT_TTL
        elif reason in _STRONG_INACTIVE_REASONS:
            state = "inactive"
            confidence = float(observation.get("confidence") or 0.9)
            failures = previous_failures + 1
            evidence_at = previous_evidence
            next_check = now + INACTIVE_TTL
        else:
            failures = previous_failures + 1
            state = "recently_observed" if previous_evidence and reason in _TRANSIENT_REASONS else "uncertain"
            confidence = 0.5 if state == "recently_observed" else float(observation.get("confidence") or 0.25)
            evidence_at = previous_evidence
            next_check = now + (RECENT_TTL if state == "recently_observed" else UNCERTAIN_TTL)

        with self.connect() as connection:
            connection.execute("""
                INSERT INTO company_vitality (
                    company_id, domain, normalized_name, state, confidence, dns_status,
                    http_status, final_url, page_title, is_parked, identity_score, reason,
                    checked_at, last_public_evidence_at, consecutive_failures, next_check_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(company_id) DO UPDATE SET
                    domain = excluded.domain,
                    normalized_name = excluded.normalized_name,
                    state = excluded.state,
                    confidence = excluded.confidence,
                    dns_status = excluded.dns_status,
                    http_status = excluded.http_status,
                    final_url = excluded.final_url,
                    page_title = excluded.page_title,
                    is_parked = excluded.is_parked,
                    identity_score = excluded.identity_score,
                    reason = excluded.reason,
                    checked_at = excluded.checked_at,
                    last_public_evidence_at = excluded.last_public_evidence_at,
                    consecutive_failures = excluded.consecutive_failures,
                    next_check_at = excluded.next_check_at,
                    updated_at = excluded.updated_at
            """, (
                company_id, str(task["domain"]), str(task.get("normalized_name") or ""),
                state, confidence, str(observation.get("dns_status") or ""),
                observation.get("http_status"), str(observation.get("final_url") or ""),
                str(observation.get("page_title") or "")[:500], int(bool(observation.get("is_parked"))),
                float(observation.get("identity_score") or 0.0), reason, checked_at,
                evidence_at or None, failures, iso_at(next_check), iso_at(now),
            ))
            connection.execute("DELETE FROM vitality_queue WHERE company_id = ?", (company_id,))

    def enqueue_due(self, limit: int = 100) -> int:
        now_text = iso_at()
        inserted = 0
        with self.connect() as connection:
            queue_size = int(connection.execute("SELECT count(*) FROM vitality_queue").fetchone()[0])
            remaining = max(0, min(limit, MAX_QUEUE_SIZE - queue_size))
            if not remaining:
                return 0
            rows = connection.execute("""
                SELECT company_id, domain, normalized_name FROM company_vitality
                WHERE next_check_at IS NOT NULL AND next_check_at <= ?
                  AND company_id NOT IN (SELECT company_id FROM vitality_queue)
                ORDER BY next_check_at ASC LIMIT ?
            """, (now_text, remaining)).fetchall()
            for row in rows:
                connection.execute("""
                    INSERT OR IGNORE INTO vitality_queue (
                        company_id, domain, normalized_name, available_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    row["company_id"], row["domain"], row["normalized_name"],
                    now_text, now_text, now_text,
                ))
                inserted += 1
        return inserted

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
        return {"enabled": True, "states": states, "queued": queued, "checking": checking}

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


def classify_page(company_name: str, domain: str, body: bytes, content_type: str = "") -> dict[str, object]:
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
    matches = sum(1 for token in unique_tokens if token in evidence_text)
    identity_score = matches / len(unique_tokens) if unique_tokens else 0.0
    compact_name = "".join(unique_tokens)
    compact_text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", evidence_text[:30_000])
    if compact_name and len(compact_name) >= 4 and compact_name in compact_text:
        identity_score = 1.0
    domain_token = domain.split(".", 1)[0].replace("-", "").replace("_", "")
    compact_tokens = "".join(unique_tokens)
    if domain_token and compact_tokens and len(domain_token) >= 4:
        if domain_token in compact_tokens or compact_tokens in domain_token:
            identity_score = max(identity_score, 0.7)

    if identity_score >= 0.45:
        return {
            "state": "active_verified", "confidence": min(0.98, 0.7 + identity_score * 0.28),
            "reason": "website_identity_match", "is_parked": False,
            "identity_score": identity_score, "page_title": title,
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

        result = classify_page(str(task.get("normalized_name") or ""), domain, body, content_type)
        result.update({
            "dns_status": "ok", "http_status": status, "final_url": final_url,
            "checked_at": checked_at,
        })
        return result

    return {
        "state": "uncertain", "confidence": 0.2, "reason": last_reason,
        "dns_status": "ok", "checked_at": checked_at,
    }
