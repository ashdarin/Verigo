"""Bounded, domain-only enrichment for disposable-email classification."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

import httpx

from app.config import settings
from app.db.jobs import job_store, utc_now


_CACHE_PREFIX = "email-risk:disposable:v1:"
_MEMORY_CACHE: dict[str, tuple[bool | None, float]] = {}
_LOCK = threading.Lock()
_BACKGROUND_EXECUTOR: ThreadPoolExecutor | None = None
_BACKGROUND_IN_FLIGHT: set[str] = set()
_BACKGROUND_SLOTS = threading.BoundedSemaphore(
    settings.disposable_lookup_background_workers + settings.disposable_lookup_background_queue
)


def _cache_key(domain: str) -> str:
    return f"{_CACHE_PREFIX}{domain}"


def _read_memory(domain: str) -> bool | None | object:
    with _LOCK:
        cached = _MEMORY_CACHE.get(domain)
        if cached is None:
            return _MISSING
        value, expires_at = cached
        if expires_at > time.monotonic():
            return value
        _MEMORY_CACHE.pop(domain, None)
    return _MISSING


def _write_memory(domain: str, value: bool | None, seconds: int) -> None:
    with _LOCK:
        _MEMORY_CACHE[domain] = (value, time.monotonic() + max(1, seconds))


_MISSING = object()


def _read_shared(domain: str) -> tuple[bool, int] | None:
    """Read a cross-worker cache entry without creating a schema dependency."""
    try:
        raw_value = job_store.service_state_value(_cache_key(domain))
    except Exception:
        return None
    if not raw_value:
        return None
    try:
        cached = json.loads(raw_value)
        expires_at = utc_now().fromisoformat(str(cached["expires_at"]))
        if expires_at <= utc_now() or not isinstance(cached["disposable"], bool):
            return None
        return bool(cached["disposable"]), max(1, int((expires_at - utc_now()).total_seconds()))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _write_shared(domain: str, disposable: bool, seconds: int) -> None:
    expires_at = utc_now() + timedelta(seconds=seconds)
    value = json.dumps({"disposable": disposable, "expires_at": expires_at.isoformat()})
    try:
        job_store.set_service_state_value(_cache_key(domain), value)
    except Exception:
        # Enrichment is best effort. Verification must not depend on this store.
        return


def lookup_disposable_domain(domain: str, *, allow_network: bool = True) -> bool | None:
    """Return a cached API verdict, or ``None`` when no verdict is available.

    Only ``probe@domain`` is sent to the provider.  The timeout, cache and
    lack of retries keep this advisory lookup off the SMTP critical path.
    """
    normalized = str(domain or "").strip().lower().rstrip(".")
    if not settings.disposable_lookup_enabled or not normalized or "." not in normalized:
        return None

    in_memory = _read_memory(normalized)
    if in_memory is not _MISSING:
        return in_memory  # type: ignore[return-value]
    shared = _read_shared(normalized)
    if shared is not None:
        verdict, seconds = shared
        _write_memory(normalized, verdict, seconds)
        return verdict
    if not allow_network:
        return None

    try:
        response = httpx.get(
            settings.disposable_lookup_url,
            params={"email": f"probe@{normalized}"},
            timeout=settings.disposable_lookup_timeout_seconds,
            follow_redirects=False,
        )
        response.raise_for_status()
        payload: Any = response.json()
        raw_verdict = payload.get("disposable") if isinstance(payload, dict) else None
        if isinstance(raw_verdict, str):
            raw_verdict = raw_verdict.strip().lower()
            if raw_verdict in {"true", "false"}:
                disposable = raw_verdict == "true"
            else:
                return None
        elif isinstance(raw_verdict, bool):
            disposable = raw_verdict
        else:
            return None
    except (httpx.HTTPError, TypeError, ValueError):
        _write_memory(normalized, None, settings.disposable_lookup_failure_cache_seconds)
        return None

    ttl_hours = (
        settings.disposable_lookup_positive_cache_hours
        if disposable else settings.disposable_lookup_negative_cache_hours
    )
    ttl_seconds = ttl_hours * 60 * 60
    _write_memory(normalized, disposable, ttl_seconds)
    _write_shared(normalized, disposable, ttl_seconds)
    return disposable


def _background_lookup(domain: str) -> None:
    try:
        lookup_disposable_domain(domain)
    finally:
        with _LOCK:
            _BACKGROUND_IN_FLIGHT.discard(domain)
        _BACKGROUND_SLOTS.release()


def prefetch_disposable_domain(domain: str) -> None:
    """Warm an unknown domain without adding latency to SMTP verification.

    The bounded executor avoids unbounded queued work for high-cardinality
    lists.  Its result is written to the normal shared cache for later rows
    and other worker processes.
    """
    normalized = str(domain or "").strip().lower().rstrip(".")
    if not settings.disposable_lookup_enabled or not normalized or "." not in normalized:
        return
    # A cached ``None`` is a deliberate short failure cache. Keep that
    # distinct from a true cache miss so an outage does not cause one request
    # per email address.
    if _read_memory(normalized) is not _MISSING:
        return
    shared = _read_shared(normalized)
    if shared is not None:
        verdict, seconds = shared
        _write_memory(normalized, verdict, seconds)
        return
    if not _BACKGROUND_SLOTS.acquire(blocking=False):
        return
    global _BACKGROUND_EXECUTOR
    with _LOCK:
        if normalized in _BACKGROUND_IN_FLIGHT:
            _BACKGROUND_SLOTS.release()
            return
        _BACKGROUND_IN_FLIGHT.add(normalized)
        if _BACKGROUND_EXECUTOR is None:
            _BACKGROUND_EXECUTOR = ThreadPoolExecutor(
                max_workers=settings.disposable_lookup_background_workers,
                thread_name_prefix="disposable-lookup",
            )
        executor = _BACKGROUND_EXECUTOR
    executor.submit(_background_lookup, normalized)


def clear_memory_cache_for_tests() -> None:
    with _LOCK:
        _MEMORY_CACHE.clear()
