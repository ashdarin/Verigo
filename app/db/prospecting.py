"""Persistent metadata for the private domain prospecting beta."""
from __future__ import annotations

import sqlite3
import threading
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
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
                connection.execute("CREATE INDEX IF NOT EXISTS idx_prospecting_runs_owner ON prospecting_runs(owner_id, created_at DESC)")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_prospecting_candidates_run ON prospecting_candidates(run_id, original_index)")
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

    def recent(self, owner_id: str, limit: int = 10) -> list[ProspectingRun]:
        self.initialize()
        with closing(self._connect()) as connection:
            rows = connection.execute("""
                SELECT id, owner_id, domain, country, requested_pattern, verification_job_id, candidate_count, created_at,
                       profile_patterns_json, profiles_recorded_at
                FROM prospecting_runs WHERE owner_id=? ORDER BY created_at DESC LIMIT ?
            """, (owner_id, limit)).fetchall()
        return [self._run_from_row(row) for row in rows]

    def count_runs_since(self, owner_id: str, start: datetime) -> int:
        self.initialize()
        with closing(self._connect()) as connection:
            return int(connection.execute(
                "SELECT COUNT(*) FROM prospecting_runs WHERE owner_id=? AND created_at>=?",
                (owner_id, start.isoformat()),
            ).fetchone()[0])

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
