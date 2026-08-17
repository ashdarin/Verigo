from __future__ import annotations

import hashlib
import hmac
import logging
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from app.config import settings
from app.core.smtp_retry_policy import smtp_status_code
from app.db.pg_compat import connect_app, postgres_active


logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sql_timestamp(value: datetime | None) -> datetime | str | None:
    if value is None:
        return None
    return value if postgres_active() else value.isoformat()


def email_fingerprint(email: str) -> str:
    key = (settings.metrics_salt or "verigo-metrics-unconfigured").encode("utf-8")
    material = f"smtp-review|{email.strip().lower()}".encode("utf-8")
    return hmac.new(key, material, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class SMTPReviewEvent:
    id: str
    parent_job_id: str
    retry_job_id: str | None
    email_hash: str
    provider_key: str
    event_type: str
    decision_reason: str | None
    origin_execution_target: str
    review_execution_target: str | None
    retry_route: str
    attempt: int
    initial_smtp_code: str | None
    review_smtp_code: str | None
    outcome: str | None
    occurred_at: datetime
    initial_completed_at: datetime | None = None
    review_started_at: datetime | None = None
    review_completed_at: datetime | None = None
    latency_ms: int | None = None


def make_smtp_review_event(
    *,
    parent_job_id: str,
    retry_job_id: str | None,
    email: str,
    provider_key: str,
    event_type: str,
    decision_reason: str | None,
    origin_execution_target: str,
    review_execution_target: str | None,
    retry_route: str,
    attempt: int,
    initial_result: dict[str, Any] | None = None,
    review_result: dict[str, Any] | None = None,
    outcome: str | None = None,
    occurred_at: datetime | None = None,
    initial_completed_at: datetime | None = None,
    review_started_at: datetime | None = None,
    review_completed_at: datetime | None = None,
    latency_ms: int | None = None,
) -> SMTPReviewEvent:
    email_hash = email_fingerprint(email)
    identity = "|".join((
        parent_job_id,
        retry_job_id or "",
        email_hash,
        event_type,
        str(max(0, int(attempt))),
    ))
    event_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:40]
    return SMTPReviewEvent(
        id=event_id,
        parent_job_id=parent_job_id,
        retry_job_id=retry_job_id,
        email_hash=email_hash,
        provider_key=provider_key,
        event_type=event_type,
        decision_reason=decision_reason,
        origin_execution_target=origin_execution_target,
        review_execution_target=review_execution_target,
        retry_route=retry_route,
        attempt=max(0, int(attempt)),
        initial_smtp_code=smtp_status_code(initial_result or {}),
        review_smtp_code=smtp_status_code(review_result or {}),
        outcome=outcome,
        occurred_at=occurred_at or utc_now(),
        initial_completed_at=initial_completed_at,
        review_started_at=review_started_at,
        review_completed_at=review_completed_at,
        latency_ms=max(0, int(latency_ms)) if latency_ms is not None else None,
    )


class SMTPReviewEventStore:
    _INSERT = """
        INSERT INTO smtp_review_events(
            id, parent_job_id, retry_job_id, email_hash, provider_key,
            event_type, decision_reason, origin_execution_target,
            review_execution_target, retry_route, attempt,
            initial_smtp_code, review_smtp_code, outcome, occurred_at,
            initial_completed_at, review_started_at, review_completed_at,
            latency_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO NOTHING
    """

    def _connect(self):
        return connect_app()

    def record_many(self, events: Iterable[SMTPReviewEvent]) -> bool:
        rows = list(events)
        if not rows:
            return True
        parameters = [(
            event.id,
            event.parent_job_id,
            event.retry_job_id,
            event.email_hash,
            event.provider_key,
            event.event_type,
            event.decision_reason,
            event.origin_execution_target,
            event.review_execution_target,
            event.retry_route,
            event.attempt,
            event.initial_smtp_code,
            event.review_smtp_code,
            event.outcome,
            _sql_timestamp(event.occurred_at),
            _sql_timestamp(event.initial_completed_at),
            _sql_timestamp(event.review_started_at),
            _sql_timestamp(event.review_completed_at),
            event.latency_ms,
        ) for event in rows]
        try:
            with closing(self._connect()) as connection:
                connection.executemany(self._INSERT, parameters)
                connection.commit()
        except Exception:  # noqa: BLE001 - telemetry must not stop verification
            logger.exception("Could not persist %s SMTP review events", len(rows))
            return False
        return True

    def report(self, days: int = 7) -> dict[str, Any]:
        days = max(1, min(90, int(days)))
        cutoff = utc_now() - timedelta(days=days)
        with closing(self._connect()) as connection:
            event_rows = connection.execute(
                """SELECT event_type, COALESCE(outcome, ''), COUNT(*)
                FROM smtp_review_events WHERE occurred_at>=?
                GROUP BY event_type, COALESCE(outcome, '')
                ORDER BY event_type, COALESCE(outcome, '')""",
                (_sql_timestamp(cutoff),),
            ).fetchall()
            decision_rows = connection.execute(
                """SELECT COALESCE(decision_reason, ''), COUNT(*)
                FROM smtp_review_events WHERE occurred_at>=?
                GROUP BY COALESCE(decision_reason, '') ORDER BY COUNT(*) DESC""",
                (_sql_timestamp(cutoff),),
            ).fetchall()
            provider_rows = connection.execute(
                """SELECT provider_key, COUNT(*)
                FROM smtp_review_events
                WHERE occurred_at>=? AND event_type='scheduled'
                GROUP BY provider_key ORDER BY COUNT(*) DESC LIMIT 20""",
                (_sql_timestamp(cutoff),),
            ).fetchall()
            latency = connection.execute(
                """SELECT COUNT(*), COALESCE(AVG(latency_ms), 0),
                    COALESCE(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY latency_ms), 0),
                    COALESCE(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms), 0)
                FROM smtp_review_events
                WHERE occurred_at>=? AND event_type='completed' AND latency_ms IS NOT NULL""",
                (_sql_timestamp(cutoff),),
            ).fetchone()
        totals: dict[str, int] = {}
        outcomes: dict[str, int] = {}
        for event_type, outcome, count in event_rows:
            totals[str(event_type)] = totals.get(str(event_type), 0) + int(count)
            if outcome:
                outcomes[str(outcome)] = outcomes.get(str(outcome), 0) + int(count)
        return {
            "days": days,
            "totals": totals,
            "outcomes": outcomes,
            "decisions": {str(reason): int(count) for reason, count in decision_rows if reason},
            "providers": {str(provider): int(count) for provider, count in provider_rows},
            "latency_ms": {
                "samples": int(latency[0] or 0) if latency else 0,
                "average": round(float(latency[1] or 0), 1) if latency else 0.0,
                "p50": round(float(latency[2] or 0), 1) if latency else 0.0,
                "p95": round(float(latency[3] or 0), 1) if latency else 0.0,
            },
        }


smtp_review_event_store = SMTPReviewEventStore()
