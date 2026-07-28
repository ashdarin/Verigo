"""Persistent metadata for the private domain prospecting beta."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from app.config import settings
from app.core.prospecting import ProspectingCandidate
from app.core.prospecting_protection import (
    control_sample_rejected,
    is_confirmed_smtp_sample,
    is_suspicious_recipient_rejection,
)
from app.db.sqlite import begin_immediate, connect


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ProspectingRun:
    id: str
    owner_id: str
    domain: str
    country: str
    requested_pattern: str | None
    verification_job_id: str
    candidate_count: int
    created_at: datetime
    profile_patterns: tuple[str, ...]
    profiles_recorded_at: datetime | None


class ProspectingStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        return connect(settings.database_path)

    def initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            with closing(self._connect()) as connection:
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS prospecting_runs (
                        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, domain TEXT NOT NULL,
                        country TEXT NOT NULL DEFAULT 'US', requested_pattern TEXT,
                        verification_job_id TEXT NOT NULL UNIQUE, candidate_count INTEGER NOT NULL,
                        profile_patterns_json TEXT NOT NULL, created_at TEXT NOT NULL,
                        profiles_recorded_at TEXT
                    )
                """)
                columns = {row[1] for row in connection.execute("PRAGMA table_info(prospecting_runs)")}
                if "country" not in columns:
                    connection.execute("ALTER TABLE prospecting_runs ADD COLUMN country TEXT NOT NULL DEFAULT 'US'")
                if "requested_pattern" not in columns:
                    connection.execute("ALTER TABLE prospecting_runs ADD COLUMN requested_pattern TEXT")
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS prospecting_candidates (
                        run_id TEXT NOT NULL, original_index INTEGER NOT NULL, email TEXT NOT NULL,
                        category TEXT NOT NULL, pattern TEXT NOT NULL, rank INTEGER NOT NULL,
                        source TEXT NOT NULL, PRIMARY KEY(run_id, original_index),
                        FOREIGN KEY(run_id) REFERENCES prospecting_runs(id) ON DELETE CASCADE
                    )
                """)
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS prospecting_domain_profiles (
                        domain TEXT NOT NULL, pattern TEXT NOT NULL, confirmed_count INTEGER NOT NULL DEFAULT 0,
                        last_confirmed_at TEXT NOT NULL, PRIMARY KEY(domain, pattern)
                    )
                """)
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS prospecting_owner_domain_profiles (
                        owner_id TEXT NOT NULL, domain TEXT NOT NULL, pattern TEXT NOT NULL,
                        confirmed_count INTEGER NOT NULL DEFAULT 0, last_confirmed_at TEXT NOT NULL,
                        PRIMARY KEY(owner_id, domain, pattern)
                    )
                """)
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS prospecting_saved_contacts (
                        owner_id TEXT NOT NULL, email TEXT NOT NULL, domain TEXT NOT NULL,
                        category TEXT NOT NULL, pattern TEXT NOT NULL, source TEXT NOT NULL,
                        run_id TEXT NOT NULL, saved_at TEXT NOT NULL,
                        PRIMARY KEY(owner_id, email),
                        FOREIGN KEY(run_id) REFERENCES prospecting_runs(id) ON DELETE CASCADE
                    )
                """)
                saved_contact_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(prospecting_saved_contacts)")
                }
                for name, definition in (
                    ("last_verified_at", "TEXT"),
                    ("verification_method", "TEXT"),
                    ("verification_detail", "TEXT"),
                    ("confidence", "INTEGER NOT NULL DEFAULT 0"),
                    ("favorite", "INTEGER NOT NULL DEFAULT 0"),
                    ("tags_json", "TEXT NOT NULL DEFAULT '[]'"),
                ):
                    if name not in saved_contact_columns:
                        connection.execute(
                            f"ALTER TABLE prospecting_saved_contacts ADD COLUMN {name} {definition}"
                        )
                connection.execute("""
                    UPDATE prospecting_saved_contacts
                    SET last_verified_at=COALESCE(last_verified_at, saved_at),
                        confidence=CASE WHEN confidence=0 THEN 80 ELSE confidence END
                    WHERE last_verified_at IS NULL OR confidence=0
                """)
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS prospecting_contact_events (
                        owner_id TEXT NOT NULL, email TEXT NOT NULL, run_id TEXT NOT NULL,
                        verified_at TEXT NOT NULL, verification_method TEXT, verification_detail TEXT,
                        confidence INTEGER NOT NULL, source TEXT NOT NULL,
                        PRIMARY KEY(owner_id, email, run_id)
                    )
                """)
                connection.execute("CREATE INDEX IF NOT EXISTS idx_prospecting_runs_owner ON prospecting_runs(owner_id, created_at DESC)")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_prospecting_runs_owner_domain ON prospecting_runs(owner_id, domain)")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_prospecting_candidates_run ON prospecting_candidates(run_id, original_index)")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_prospecting_saved_contacts_owner ON prospecting_saved_contacts(owner_id, saved_at DESC)")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_prospecting_saved_contacts_owner_domain ON prospecting_saved_contacts(owner_id, domain, saved_at DESC)")
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS prospecting_companies (
                        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, import_id TEXT NOT NULL,
                        name TEXT NOT NULL, domain TEXT, country TEXT, industry TEXT,
                        source_row INTEGER NOT NULL, selected INTEGER NOT NULL DEFAULT 0,
                        discovery_run_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                        UNIQUE(owner_id, import_id, name, domain)
                    )
                """)
                connection.execute("CREATE INDEX IF NOT EXISTS idx_prospecting_companies_owner ON prospecting_companies(owner_id, created_at DESC)")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_prospecting_companies_owner_import ON prospecting_companies(owner_id, import_id, id)")
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS prospecting_control_samples (
                        owner_id TEXT NOT NULL, domain TEXT NOT NULL, email TEXT NOT NULL,
                        verified_at TEXT NOT NULL, smtp_detail TEXT NOT NULL,
                        PRIMARY KEY(owner_id, domain)
                    )
                """)
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS prospecting_domain_protection (
                        domain TEXT PRIMARY KEY, pressure_events INTEGER NOT NULL DEFAULT 0,
                        strong_events INTEGER NOT NULL DEFAULT 0, cooldown_until TEXT,
                        stop_until TEXT, last_reason TEXT, updated_at TEXT NOT NULL
                    )
                """)
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS prospecting_protection_events (
                        run_id TEXT NOT NULL, original_index INTEGER NOT NULL, signal TEXT NOT NULL,
                        created_at TEXT NOT NULL, PRIMARY KEY(run_id, original_index),
                        FOREIGN KEY(run_id) REFERENCES prospecting_runs(id) ON DELETE CASCADE
                    )
                """)
                # A generic 550 is not proof of enumeration. Clear the old
                # automatic blocks; future stops require a failed control sample.
                connection.execute("""
                    UPDATE prospecting_domain_protection
                    SET stop_until=NULL, cooldown_until=NULL,
                        last_reason='Legacy generic 550 protection cleared; a control sample is now required',
                        updated_at=?
                    WHERE last_reason='The receiving mail system returned repeated non-specific 550 refusals'
                """, (utc_now().isoformat(),))
            self._initialized = True

    @staticmethod
    def _run_from_row(row: tuple[Any, ...]) -> ProspectingRun:
        return ProspectingRun(
            id=row[0], owner_id=row[1], domain=row[2], country=row[3], requested_pattern=row[4],
            verification_job_id=row[5], candidate_count=int(row[6]), created_at=datetime.fromisoformat(row[7]),
            profile_patterns=tuple(json.loads(row[8])),
            profiles_recorded_at=datetime.fromisoformat(row[9]) if row[9] else None,
        )

    def create_run(
        self,
        owner_id: str,
        domain: str,
        country: str,
        requested_pattern: str | None,
        verification_job_id: str,
        candidates: list[ProspectingCandidate],
        profile_patterns: Iterable[str],
    ) -> ProspectingRun:
        self.initialize()
        run = ProspectingRun(
            id=uuid.uuid4().hex[:12], owner_id=owner_id, domain=domain, country=country,
            requested_pattern=requested_pattern,
            verification_job_id=verification_job_id, candidate_count=len(candidates),
            created_at=utc_now(), profile_patterns=tuple(profile_patterns), profiles_recorded_at=None,
        )
        with closing(self._connect()) as connection:
            begin_immediate(connection)
            try:
                connection.execute("""
                    INSERT INTO prospecting_runs(
                        id, owner_id, domain, country, requested_pattern, verification_job_id, candidate_count,
                        profile_patterns_json, created_at, profiles_recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """, (
                    run.id, run.owner_id, run.domain, run.country, run.requested_pattern, run.verification_job_id,
                    run.candidate_count, json.dumps(run.profile_patterns), run.created_at.isoformat(),
                ))
                connection.executemany("""
                    INSERT INTO prospecting_candidates(run_id, original_index, email, category, pattern, rank, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, [
                    (run.id, index, item.email, item.category, item.pattern, item.rank, item.source)
                    for index, item in enumerate(candidates)
                ])
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return run

    def get(self, run_id: str, owner_id: str) -> ProspectingRun | None:
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute("""
                SELECT id, owner_id, domain, country, requested_pattern, verification_job_id, candidate_count, created_at,
                       profile_patterns_json, profiles_recorded_at
                FROM prospecting_runs WHERE id=? AND owner_id=?
            """, (run_id, owner_id)).fetchone()
        return self._run_from_row(row) if row else None

    def get_by_job_id(self, verification_job_id: str) -> ProspectingRun | None:
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute("""
                SELECT id, owner_id, domain, country, requested_pattern, verification_job_id, candidate_count, created_at,
                       profile_patterns_json, profiles_recorded_at
                FROM prospecting_runs WHERE verification_job_id=?
            """, (verification_job_id,)).fetchone()
        return self._run_from_row(row) if row else None

    def recent(self, owner_id: str, limit: int = 10) -> list[ProspectingRun]:
        self.initialize()
        with closing(self._connect()) as connection:
            rows = connection.execute("""
                SELECT id, owner_id, domain, country, requested_pattern, verification_job_id, candidate_count, created_at,
                       profile_patterns_json, profiles_recorded_at
                FROM prospecting_runs WHERE owner_id=? ORDER BY created_at DESC LIMIT ?
            """, (owner_id, limit)).fetchall()
        return [self._run_from_row(row) for row in rows]

    def issued_emails(self, owner_id: str, domain: str) -> set[str]:
        self.initialize()
        with closing(self._connect()) as connection:
            rows = connection.execute("""
                SELECT candidate.email
                FROM prospecting_candidates AS candidate
                JOIN prospecting_runs AS run ON run.id=candidate.run_id
                WHERE run.owner_id=? AND run.domain=?
            """, (owner_id, domain)).fetchall()
        return {str(row[0]) for row in rows}

    def candidates(self, run_id: str) -> list[dict[str, Any]]:
        self.initialize()
        with closing(self._connect()) as connection:
            rows = connection.execute("""
                SELECT original_index, email, category, pattern, rank, source
                FROM prospecting_candidates WHERE run_id=? ORDER BY original_index
            """, (run_id,)).fetchall()
        return [
            {"original_index": int(row[0]), "email": row[1], "category": row[2],
             "pattern": row[3], "rank": int(row[4]), "source": row[5]}
            for row in rows
        ]

    def result_count(self, run_id: str) -> int:
        self.initialize()
        with closing(self._connect()) as connection:
            return int(connection.execute("""
                SELECT COUNT(*) FROM prospecting_candidates AS candidate
                JOIN prospecting_runs AS run ON run.id=candidate.run_id
                JOIN job_results AS result ON result.job_id=run.verification_job_id
                    AND result.original_index=candidate.original_index
                WHERE candidate.run_id=? AND (result.deliverability=1 OR result.is_catch_all=1)
            """, (run_id,)).fetchone()[0])

    def result_page(
        self, run: ProspectingRun, *, offset: int = 0, limit: int = 50,
    ) -> tuple[int, list[dict[str, Any]]]:
        """Return only user-visible confirmations, without hydrating every candidate."""
        total = self.result_count(run.id)
        with closing(self._connect()) as connection:
            rows = connection.execute("""
                SELECT candidate.original_index, candidate.email, candidate.category, candidate.pattern,
                       candidate.rank, candidate.source, result.result_json
                FROM prospecting_candidates AS candidate
                JOIN job_results AS result ON result.job_id=?
                    AND result.original_index=candidate.original_index
                WHERE candidate.run_id=? AND (result.deliverability=1 OR result.is_catch_all=1)
                ORDER BY candidate.rank LIMIT ? OFFSET ?
            """, (run.verification_job_id, run.id, limit, offset)).fetchall()
        items = []
        for row in rows:
            raw = json.loads(row[6])
            items.append({
                "original_index": int(row[0]), "email": row[1], "category": row[2],
                "pattern": row[3], "rank": int(row[4]), "source": row[5],
                "verification": raw,
                "result_type": "catch_all" if raw.get("domain_type") == "catch-all" else "verified",
            })
        return total, items

    def domain_patterns(self, owner_id: str, domain: str) -> list[str]:
        self.initialize()
        with closing(self._connect()) as connection:
            rows = connection.execute("""
                SELECT pattern FROM prospecting_owner_domain_profiles WHERE owner_id=? AND domain=?
                ORDER BY confirmed_count DESC, last_confirmed_at DESC LIMIT 3
            """, (owner_id, domain)).fetchall()
        return [str(row[0]) for row in rows]

    def record_provided_pattern(self, owner_id: str, domain: str, pattern: str) -> None:
        """Retain a user-supplied naming rule only inside that user's workspace."""
        self.initialize()
        with closing(self._connect()) as connection:
            begin_immediate(connection)
            try:
                connection.execute("""
                    INSERT INTO prospecting_owner_domain_profiles(
                        owner_id, domain, pattern, confirmed_count, last_confirmed_at
                    ) VALUES (?, ?, ?, 1, ?)
                    ON CONFLICT(owner_id, domain, pattern) DO UPDATE SET
                        confirmed_count=confirmed_count + 1, last_confirmed_at=excluded.last_confirmed_at
                """, (owner_id, domain, pattern, utc_now().isoformat()))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def save_confirmed_contacts(self, run: ProspectingRun, results: list[dict[str, Any]]) -> int:
        """Persist user-owned, non-catch-all confirmed contacts idempotently."""
        candidate_by_index = {item["original_index"]: item for item in self.candidates(run.id)}
        verified_at = utc_now().isoformat()
        rows = []
        event_rows = []
        control_rows = []
        for result in results:
            if result.get("deliverable") is not True or result.get("domain_type") == "catch-all":
                continue
            candidate = candidate_by_index.get(int(result.get("original_index", -1)))
            if candidate is None:
                continue
            method = str(result.get("verification_method") or "")
            detail = str(result.get("smtp_result") or result.get("message") or "")[:500]
            checks = result.get("checks") if isinstance(result.get("checks"), dict) else {}
            confidence = 100 if checks.get("smtp") is True else 90
            rows.append((
                run.owner_id, candidate["email"], run.domain, candidate["category"],
                candidate["pattern"], candidate["source"], run.id, verified_at,
                verified_at, method, detail, confidence,
            ))
            event_rows.append((
                run.owner_id, candidate["email"], run.id, verified_at, method, detail,
                confidence, candidate["source"],
            ))
            if is_confirmed_smtp_sample(result):
                control_rows.append((
                    run.owner_id, run.domain, candidate["email"], verified_at,
                    str(result.get("smtp_raw_result") or result.get("smtp_result") or "250")[:500],
                ))
        if not rows:
            return 0
        with closing(self._connect()) as connection:
            begin_immediate(connection)
            try:
                before = connection.total_changes
                connection.executemany("""
                    INSERT INTO prospecting_saved_contacts(
                        owner_id, email, domain, category, pattern, source, run_id, saved_at,
                        last_verified_at, verification_method, verification_detail, confidence
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(owner_id, email) DO UPDATE SET
                        domain=excluded.domain, category=excluded.category, pattern=excluded.pattern,
                        source=excluded.source, run_id=excluded.run_id,
                        last_verified_at=excluded.last_verified_at,
                        verification_method=excluded.verification_method,
                        verification_detail=excluded.verification_detail,
                        confidence=MAX(prospecting_saved_contacts.confidence, excluded.confidence)
                """, rows)
                inserted = connection.total_changes - before
                connection.executemany("""
                    INSERT INTO prospecting_contact_events(
                        owner_id, email, run_id, verified_at, verification_method,
                        verification_detail, confidence, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(owner_id, email, run_id) DO NOTHING
                """, event_rows)
                if control_rows:
                    connection.executemany("""
                        INSERT INTO prospecting_control_samples(owner_id, domain, email, verified_at, smtp_detail)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(owner_id, domain) DO UPDATE SET
                            email=excluded.email, verified_at=excluded.verified_at,
                            smtp_detail=excluded.smtp_detail
                    """, control_rows)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return inserted

    def control_sample_for_job(self, verification_job_id: str) -> str | None:
        """Return this user's newest known SMTP-250 sample for a discovery run."""
        run = self.get_by_job_id(verification_job_id)
        if run is None:
            return None
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute("""
                SELECT email FROM prospecting_control_samples WHERE owner_id=? AND domain=?
            """, (run.owner_id, run.domain)).fetchone()
        return str(row[0]) if row else None

    def _protection_status(self, domain: str, now: datetime | None = None) -> dict[str, Any]:
        self.initialize()
        now = now or utc_now()
        with closing(self._connect()) as connection:
            row = connection.execute("""
                SELECT pressure_events, strong_events, cooldown_until, stop_until, last_reason
                FROM prospecting_domain_protection WHERE domain=?
            """, (domain,)).fetchone()
        if row is None:
            return {"state": "clear", "resume_at": None, "message": None}
        cooldown_until = datetime.fromisoformat(row[2]) if row[2] else None
        stop_until = datetime.fromisoformat(row[3]) if row[3] else None
        if stop_until and stop_until > now:
            return {
                "state": "stopped", "resume_at": stop_until.isoformat(), "message": row[4],
                "pressure_events": int(row[0]), "strong_events": int(row[1]),
            }
        if cooldown_until and cooldown_until > now:
            return {
                "state": "waiting", "resume_at": cooldown_until.isoformat(), "message": row[4],
                "pressure_events": int(row[0]), "strong_events": int(row[1]),
            }
        return {
            "state": "clear", "resume_at": None, "message": None,
            "pressure_events": int(row[0]), "strong_events": int(row[1]),
        }

    def protection_status(self, domain: str) -> dict[str, Any]:
        return self._protection_status(domain)

    def blocked_until(self, domain: str) -> datetime | None:
        status = self._protection_status(domain)
        if status["state"] != "stopped" or not status["resume_at"]:
            return None
        return datetime.fromisoformat(str(status["resume_at"]))

    def apply_protection_outcomes(
        self, verification_job_id: str, results: list[dict[str, Any]],
        control_probes: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """Use a failed SMTP-250 control sample as the sole hard stop signal."""
        run = self.get_by_job_id(verification_job_id)
        if run is None:
            return None
        if any(result.get("deliverable") is True and result.get("domain_type") != "catch-all" for result in results):
            self.save_confirmed_contacts(run, results)
        signals: list[tuple[int, str, str]] = []
        suspicious = any(is_suspicious_recipient_rejection(result) for result in results)
        sample = self.control_sample_for_job(verification_job_id)
        if suspicious and sample:
            for probe in control_probes or []:
                if str(probe.get("email") or "").lower() != sample.lower():
                    continue
                raw_probe = probe.get("result")
                if isinstance(raw_probe, dict) and control_sample_rejected(raw_probe):
                    signals.append((
                        -1, "stop",
                        "A previously SMTP-250 control address now returned 550 during recipient discovery",
                    ))
                    break
        if not signals:
            return None

        now = utc_now()
        new_signals: list[tuple[int, str, str]] = []
        with closing(self._connect()) as connection:
            begin_immediate(connection)
            try:
                connection.execute("""
                    INSERT INTO prospecting_domain_protection(domain, updated_at)
                    VALUES (?, ?) ON CONFLICT(domain) DO NOTHING
                """, (run.domain, now.isoformat()))
                for index, signal, reason in signals:
                    inserted = connection.execute("""
                        INSERT INTO prospecting_protection_events(run_id, original_index, signal, created_at)
                        VALUES (?, ?, ?, ?) ON CONFLICT(run_id, original_index) DO NOTHING
                    """, (run.id, index, signal, now.isoformat())).rowcount
                    if inserted:
                        new_signals.append((index, signal, reason))
                if not new_signals:
                    connection.execute("COMMIT")
                    return None
                strong_count = sum(signal == "stop" for _, signal, _ in new_signals)
                # SMTP 4xx and generic 550 responses describe an individual
                # request, not a domain-wide enumeration block. Only a known
                # SMTP-250 control mailbox changing to 550 can stop discovery.
                stop = strong_count > 0
                reason = new_signals[-1][2]
                stop_until = now + timedelta(seconds=settings.prospecting_protection_stop_seconds)
                connection.execute("""
                    UPDATE prospecting_domain_protection SET
                        pressure_events=pressure_events+?, strong_events=strong_events+?,
                        cooldown_until=NULL,
                        stop_until=CASE WHEN ? THEN ? ELSE stop_until END,
                        last_reason=?, updated_at=? WHERE domain=?
                """, (
                    0, strong_count,
                    int(stop), stop_until.isoformat(), reason, now.isoformat(), run.domain,
                ))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        if stop:
            return {
                "action": "stop", "resume_at": stop_until, "message":
                "Discovery stopped automatically because the receiving system rejected further address checks",
            }
        return None

    def finalize_run(self, verification_job_id: str, results: list[dict[str, Any]]) -> None:
        run = self.get_by_job_id(verification_job_id)
        if run is None:
            return
        self.save_confirmed_contacts(run, results)
        self.record_confirmed_patterns(run, results)

    def saved_contact_domains(
        self, owner_id: str, *, search: str = "", offset: int = 0, limit: int = 50,
    ) -> tuple[int, list[dict[str, Any]]]:
        self.initialize()
        clauses = ["owner_id=?"]
        parameters: list[Any] = [owner_id]
        if search:
            clauses.append("(domain LIKE ? COLLATE NOCASE OR email LIKE ? COLLATE NOCASE)")
            parameters.extend((f"%{search}%", f"%{search}%"))
        where = " AND ".join(clauses)
        with closing(self._connect()) as connection:
            total = int(connection.execute(f"""
                SELECT COUNT(*) FROM (
                    SELECT domain FROM prospecting_saved_contacts WHERE {where} GROUP BY domain
                )
            """, parameters).fetchone()[0])
            rows = connection.execute(f"""
                SELECT domain, COUNT(*) AS contact_count, MAX(saved_at) AS latest_saved_at
                FROM prospecting_saved_contacts WHERE {where}
                GROUP BY domain ORDER BY contact_count DESC, latest_saved_at DESC, domain ASC
                LIMIT ? OFFSET ?
            """, [*parameters, limit, offset]).fetchall()
        return total, [
            {"domain": row[0], "contact_count": int(row[1]), "latest_saved_at": row[2]}
            for row in rows
        ]

    def saved_contacts(
        self, owner_id: str, *, domain: str | None = None, search: str = "",
        offset: int = 0, limit: int = 50,
    ) -> tuple[int, list[dict[str, Any]]]:
        self.initialize()
        clauses = ["owner_id=?"]
        parameters: list[Any] = [owner_id]
        if domain:
            clauses.append("domain=?")
            parameters.append(domain)
        if search:
            clauses.append("email LIKE ? COLLATE NOCASE")
            parameters.append(f"%{search}%")
        where = " AND ".join(clauses)
        with closing(self._connect()) as connection:
            total = int(connection.execute(
                f"SELECT COUNT(*) FROM prospecting_saved_contacts WHERE {where}", parameters
            ).fetchone()[0])
            rows = connection.execute(f"""
                SELECT email, domain, category, pattern, source, run_id, saved_at,
                       last_verified_at, verification_method, verification_detail, confidence,
                       favorite, tags_json
                FROM prospecting_saved_contacts WHERE {where}
                ORDER BY last_verified_at DESC, saved_at DESC, email ASC LIMIT ? OFFSET ?
            """, [*parameters, limit, offset]).fetchall()
        return total, [self._saved_contact_from_row(row) for row in rows]

    @staticmethod
    def _saved_contact_from_row(row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "email": row[0], "domain": row[1], "category": row[2], "pattern": row[3],
            "source": row[4], "run_id": row[5], "saved_at": row[6],
            "last_verified_at": row[7], "verification_method": row[8],
            "verification_detail": row[9], "confidence": int(row[10] or 0),
            "favorite": bool(row[11]), "tags": json.loads(row[12] or "[]"),
        }

    def update_saved_contact(
        self, owner_id: str, email: str, *, favorite: bool | None, tags: list[str] | None,
    ) -> dict[str, Any] | None:
        self.initialize()
        assignments: list[str] = []
        parameters: list[Any] = []
        if favorite is not None:
            assignments.append("favorite=?")
            parameters.append(int(favorite))
        if tags is not None:
            assignments.append("tags_json=?")
            parameters.append(json.dumps(tags, ensure_ascii=False))
        if assignments:
            with closing(self._connect()) as connection:
                connection.execute(
                    f"UPDATE prospecting_saved_contacts SET {', '.join(assignments)} "
                    "WHERE owner_id=? AND email=?",
                    [*parameters, owner_id, email.lower()],
                )
        with closing(self._connect()) as connection:
            row = connection.execute("""
                SELECT email, domain, category, pattern, source, run_id, saved_at,
                       last_verified_at, verification_method, verification_detail, confidence,
                       favorite, tags_json
                FROM prospecting_saved_contacts WHERE owner_id=? AND email=?
            """, (owner_id, email.lower())).fetchone()
        return self._saved_contact_from_row(row) if row else None

    def company_snapshot(self, owner_id: str, domain: str) -> dict[str, Any]:
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute("""
                SELECT COUNT(*), COALESCE(MAX(last_verified_at), MAX(saved_at)),
                       COALESCE(SUM(favorite), 0)
                FROM prospecting_saved_contacts WHERE owner_id=? AND domain=?
            """, (owner_id, domain)).fetchone()
        return {
            "domain": domain, "contact_count": int(row[0]), "last_verified_at": row[1],
            "favorite_count": int(row[2]),
        }

    def saved_contact_count(self, owner_id: str) -> int:
        self.initialize()
        with closing(self._connect()) as connection:
            return int(connection.execute(
                "SELECT COUNT(*) FROM prospecting_saved_contacts WHERE owner_id=?", (owner_id,)
            ).fetchone()[0])

    def import_companies(self, owner_id: str, companies: Iterable[Any]) -> tuple[str, int]:
        """Store a user-provided company source list for review before discovery."""
        self.initialize()
        import_id = uuid.uuid4().hex[:12]
        now = utc_now().isoformat()
        rows = [
            (uuid.uuid4().hex[:12], owner_id, import_id, item.name, item.domain, item.country,
             item.industry, item.source_row, now, now)
            for item in companies
        ]
        if not rows:
            return import_id, 0
        with closing(self._connect()) as connection:
            connection.executemany("""
                INSERT INTO prospecting_companies(
                    id, owner_id, import_id, name, domain, country, industry, source_row,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
        return import_id, len(rows)

    def company_page(
        self, owner_id: str, *, import_id: str | None = None, search: str = "",
        domain_state: str = "all", offset: int = 0, limit: int = 50,
    ) -> tuple[int, list[dict[str, Any]]]:
        self.initialize()
        clauses = ["owner_id=?"]
        parameters: list[Any] = [owner_id]
        if import_id:
            clauses.append("import_id=?")
            parameters.append(import_id)
        if search:
            clauses.append("(name LIKE ? COLLATE NOCASE OR domain LIKE ? COLLATE NOCASE OR industry LIKE ? COLLATE NOCASE)")
            parameters.extend([f"%{search}%"] * 3)
        if domain_state == "ready":
            clauses.append("domain IS NOT NULL")
        elif domain_state == "missing":
            clauses.append("domain IS NULL")
        where = " AND ".join(clauses)
        with closing(self._connect()) as connection:
            total = int(connection.execute(
                f"SELECT COUNT(*) FROM prospecting_companies WHERE {where}", parameters
            ).fetchone()[0])
            rows = connection.execute(f"""
                SELECT id, import_id, name, domain, country, industry, source_row, selected,
                       discovery_run_id, created_at, updated_at
                FROM prospecting_companies WHERE {where}
                ORDER BY selected DESC, domain IS NOT NULL DESC, name COLLATE NOCASE ASC
                LIMIT ? OFFSET ?
            """, [*parameters, limit, offset]).fetchall()
        return total, [self._company_from_row(row) for row in rows]

    @staticmethod
    def _company_from_row(row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "id": row[0], "import_id": row[1], "name": row[2], "domain": row[3],
            "country": row[4], "industry": row[5], "source_row": int(row[6]),
            "selected": bool(row[7]), "discovery_run_id": row[8],
            "created_at": row[9], "updated_at": row[10],
        }

    def update_company(
        self, owner_id: str, company_id: str, *, domain: str | None,
        country: str | None, selected: bool | None,
    ) -> dict[str, Any] | None:
        self.initialize()
        assignments: list[str] = ["updated_at=?"]
        parameters: list[Any] = [utc_now().isoformat()]
        if domain is not None:
            assignments.append("domain=?")
            parameters.append(domain)
        if country is not None:
            assignments.append("country=?")
            parameters.append(country)
        if selected is not None:
            assignments.append("selected=?")
            parameters.append(int(selected))
        with closing(self._connect()) as connection:
            connection.execute(
                f"UPDATE prospecting_companies SET {', '.join(assignments)} WHERE id=? AND owner_id=?",
                [*parameters, company_id, owner_id],
            )
            row = connection.execute("""
                SELECT id, import_id, name, domain, country, industry, source_row, selected,
                       discovery_run_id, created_at, updated_at
                FROM prospecting_companies WHERE id=? AND owner_id=?
            """, (company_id, owner_id)).fetchone()
        return self._company_from_row(row) if row else None

    def selected_companies(self, owner_id: str, company_ids: list[str]) -> list[dict[str, Any]]:
        self.initialize()
        if not company_ids:
            return []
        placeholders = ", ".join("?" for _ in company_ids)
        with closing(self._connect()) as connection:
            rows = connection.execute(f"""
                SELECT id, import_id, name, domain, country, industry, source_row, selected,
                       discovery_run_id, created_at, updated_at
                FROM prospecting_companies WHERE owner_id=? AND id IN ({placeholders})
            """, [owner_id, *company_ids]).fetchall()
        return [self._company_from_row(row) for row in rows]

    def attach_company_run(self, owner_id: str, company_id: str, run_id: str) -> None:
        self.initialize()
        with closing(self._connect()) as connection:
            connection.execute("""
                UPDATE prospecting_companies SET discovery_run_id=?, selected=0, updated_at=?
                WHERE id=? AND owner_id=?
            """, (run_id, utc_now().isoformat(), company_id, owner_id))

    def record_confirmed_patterns(self, run: ProspectingRun, results: list[dict[str, Any]]) -> bool:
        """Record only non-catch-all personal confirmations, exactly once per run."""
        if run.profiles_recorded_at is not None:
            return False
        candidate_by_index = {item["original_index"]: item for item in self.candidates(run.id)}
        patterns = [
            candidate_by_index.get(int(result.get("original_index", -1)), {}).get("pattern")
            for result in results
            if result.get("deliverable") is True
            and result.get("domain_type") != "catch-all"
            and candidate_by_index.get(int(result.get("original_index", -1)), {}).get("category") == "personal_candidate"
        ]
        now = utc_now().isoformat()
        with closing(self._connect()) as connection:
            begin_immediate(connection)
            try:
                marker = connection.execute(
                    "SELECT profiles_recorded_at FROM prospecting_runs WHERE id=?", (run.id,)
                ).fetchone()
                if marker is None or marker[0] is not None:
                    connection.execute("ROLLBACK")
                    return False
                for pattern in patterns:
                    if not pattern:
                        continue
                    connection.execute("""
                        INSERT INTO prospecting_owner_domain_profiles(
                            owner_id, domain, pattern, confirmed_count, last_confirmed_at
                        ) VALUES (?, ?, ?, 1, ?)
                        ON CONFLICT(owner_id, domain, pattern) DO UPDATE SET
                            confirmed_count=confirmed_count + 1, last_confirmed_at=excluded.last_confirmed_at
                    """, (run.owner_id, run.domain, pattern, now))
                connection.execute(
                    "UPDATE prospecting_runs SET profiles_recorded_at=? WHERE id=?", (now, run.id)
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return True


prospecting_store = ProspectingStore()
