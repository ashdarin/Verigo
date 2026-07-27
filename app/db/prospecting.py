"""Persistent metadata for the private domain prospecting beta."""
from __future__ import annotations

import sqlite3
import threading
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from app.config import settings
from app.core.prospecting import ProspectingCandidate
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
                    CREATE TABLE IF NOT EXISTS prospecting_saved_contacts (
                        owner_id TEXT NOT NULL, email TEXT NOT NULL, domain TEXT NOT NULL,
                        category TEXT NOT NULL, pattern TEXT NOT NULL, source TEXT NOT NULL,
                        run_id TEXT NOT NULL, saved_at TEXT NOT NULL,
                        PRIMARY KEY(owner_id, email),
                        FOREIGN KEY(run_id) REFERENCES prospecting_runs(id) ON DELETE CASCADE
                    )
                """)
                connection.execute("CREATE INDEX IF NOT EXISTS idx_prospecting_runs_owner ON prospecting_runs(owner_id, created_at DESC)")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_prospecting_candidates_run ON prospecting_candidates(run_id, original_index)")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_prospecting_saved_contacts_owner ON prospecting_saved_contacts(owner_id, saved_at DESC)")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_prospecting_saved_contacts_owner_domain ON prospecting_saved_contacts(owner_id, domain, saved_at DESC)")
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
            self._initialized = True

    @staticmethod
    def _run_from_row(row: tuple[Any, ...]) -> ProspectingRun:
        import json
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
        import json
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

    def domain_patterns(self, domain: str) -> list[str]:
        self.initialize()
        with closing(self._connect()) as connection:
            rows = connection.execute("""
                SELECT pattern FROM prospecting_domain_profiles WHERE domain=?
                ORDER BY confirmed_count DESC, last_confirmed_at DESC LIMIT 3
            """, (domain,)).fetchall()
        return [str(row[0]) for row in rows]

    def record_provided_pattern(self, domain: str, pattern: str) -> None:
        """Retain a user-supplied known-contact pattern for future runs."""
        self.initialize()
        with closing(self._connect()) as connection:
            begin_immediate(connection)
            try:
                connection.execute("""
                    INSERT INTO prospecting_domain_profiles(domain, pattern, confirmed_count, last_confirmed_at)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(domain, pattern) DO UPDATE SET
                        confirmed_count=confirmed_count + 1, last_confirmed_at=excluded.last_confirmed_at
                """, (domain, pattern, utc_now().isoformat()))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def save_confirmed_contacts(self, run: ProspectingRun, results: list[dict[str, Any]]) -> int:
        """Persist user-owned, non-catch-all confirmed contacts idempotently."""
        candidate_by_index = {item["original_index"]: item for item in self.candidates(run.id)}
        saved_at = utc_now().isoformat()
        rows = []
        for result in results:
            if result.get("deliverable") is not True or result.get("domain_type") == "catch-all":
                continue
            candidate = candidate_by_index.get(int(result.get("original_index", -1)))
            if candidate is None:
                continue
            rows.append((
                run.owner_id, candidate["email"], run.domain, candidate["category"],
                candidate["pattern"], candidate["source"], run.id, saved_at,
            ))
        if not rows:
            return 0
        with closing(self._connect()) as connection:
            begin_immediate(connection)
            try:
                before = connection.total_changes
                connection.executemany("""
                    INSERT INTO prospecting_saved_contacts(
                        owner_id, email, domain, category, pattern, source, run_id, saved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(owner_id, email) DO NOTHING
                """, rows)
                inserted = connection.total_changes - before
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return inserted

    @staticmethod
    def _protection_signal(result: dict[str, Any]) -> tuple[str, str] | None:
        """Classify receiver feedback conservatively; mailbox failures never count."""
        detail = " ".join(
            str(result.get(field) or "")
            for field in ("smtp_raw_result", "smtp_result", "message", "failure_reason")
        ).lower()
        if not detail or result.get("delivery_block_reason") == "mailbox_full":
            return None
        strong_markers = (
            "anti-enumerat", "anti enumerat", "enumeration", "directory harvest",
            "too many invalid recipient", "too many recipients", "recipient verification",
            "verification not permitted", "verification not allowed",
        )
        if any(marker in detail for marker in strong_markers):
            return "stop", "The receiving mail system rejected further address discovery"
        pressure_markers = (
            "rate limit", "rate limited", "throttl", "too many connection", "too many request",
            "temporarily blocked", "try again later", "server busy",
        )
        code = str(result.get("smtp_code") or "")
        if code.startswith("4") or any(marker in detail for marker in pressure_markers):
            return "wait", "The receiving mail system requested a cooldown before more checks"
        return None

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
        self, verification_job_id: str, results: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Persist new receiver-pressure signals and choose wait versus stop once."""
        run = self.get_by_job_id(verification_job_id)
        if run is None:
            return None
        signals: list[tuple[int, str, str]] = []
        for fallback_index, raw in enumerate(results):
            signal = self._protection_signal(raw)
            if signal is not None:
                signals.append((int(raw.get("original_index", fallback_index)), *signal))
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
                pressure_count = sum(signal == "wait" for _, signal, _ in new_signals)
                strong_count = sum(signal == "stop" for _, signal, _ in new_signals)
                run_pressure_count = int(connection.execute("""
                    SELECT COUNT(*) FROM prospecting_protection_events WHERE run_id=? AND signal='wait'
                """, (run.id,)).fetchone()[0])
                stop = strong_count > 0 or run_pressure_count >= settings.prospecting_protection_max_pressure_events
                reason = new_signals[-1][2]
                cooldown_until = now + timedelta(seconds=settings.prospecting_protection_cooldown_seconds)
                stop_until = now + timedelta(seconds=settings.prospecting_protection_stop_seconds)
                connection.execute("""
                    UPDATE prospecting_domain_protection SET
                        pressure_events=pressure_events+?, strong_events=strong_events+?,
                        cooldown_until=CASE WHEN ? THEN cooldown_until ELSE ? END,
                        stop_until=CASE WHEN ? THEN ? ELSE stop_until END,
                        last_reason=?, updated_at=? WHERE domain=?
                """, (
                    pressure_count, strong_count, int(stop), cooldown_until.isoformat(),
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
        return {
            "action": "wait", "resume_at": cooldown_until, "message":
            "Discovery paused automatically to respect the receiving system's rate limit",
        }

    def saved_contact_domains(self, owner_id: str, search: str = "") -> list[dict[str, Any]]:
        self.initialize()
        clauses = ["owner_id=?"]
        parameters: list[Any] = [owner_id]
        if search:
            clauses.append("(domain LIKE ? COLLATE NOCASE OR email LIKE ? COLLATE NOCASE)")
            parameters.extend((f"%{search}%", f"%{search}%"))
        where = " AND ".join(clauses)
        with closing(self._connect()) as connection:
            rows = connection.execute(f"""
                SELECT domain, COUNT(*) AS contact_count, MAX(saved_at) AS latest_saved_at
                FROM prospecting_saved_contacts WHERE {where}
                GROUP BY domain ORDER BY contact_count DESC, latest_saved_at DESC, domain ASC
            """, parameters).fetchall()
        return [
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
                SELECT email, domain, category, pattern, source, run_id, saved_at
                FROM prospecting_saved_contacts WHERE {where}
                ORDER BY saved_at DESC, email ASC LIMIT ? OFFSET ?
            """, [*parameters, limit, offset]).fetchall()
        return total, [
            {"email": row[0], "domain": row[1], "category": row[2], "pattern": row[3],
             "source": row[4], "run_id": row[5], "saved_at": row[6]}
            for row in rows
        ]

    def saved_contact_count(self, owner_id: str) -> int:
        self.initialize()
        with closing(self._connect()) as connection:
            return int(connection.execute(
                "SELECT COUNT(*) FROM prospecting_saved_contacts WHERE owner_id=?", (owner_id,)
            ).fetchone()[0])

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
                        INSERT INTO prospecting_domain_profiles(domain, pattern, confirmed_count, last_confirmed_at)
                        VALUES (?, ?, 1, ?)
                        ON CONFLICT(domain, pattern) DO UPDATE SET
                            confirmed_count=confirmed_count + 1, last_confirmed_at=excluded.last_confirmed_at
                    """, (run.domain, pattern, now))
                connection.execute(
                    "UPDATE prospecting_runs SET profiles_recorded_at=? WHERE id=?", (now, run.id)
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return True


prospecting_store = ProspectingStore()
