"""Worker-process implementation for distributed email verification."""

from __future__ import annotations

import signal
import time
from contextlib import contextmanager

from app.config import settings
from app.core.email_risk import (
    enrich_disposable_provider,
    ensure_email_risk_signals,
    prefetch_disposable_provider,
)


class EmailVerificationTimeout(TimeoutError):
    pass


@contextmanager
def email_verification_deadline(seconds: int):
    """Bound one verifier call on Linux worker processes."""
    if not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
        yield
        return

    def raise_timeout(_signum, _frame):
        raise EmailVerificationTimeout(f"email verification exceeded {seconds} seconds")

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, raise_timeout)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, float(seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def timeout_result(email: str, index: int) -> dict[str, object]:
    result: dict[str, object] = {
        "email": email,
        "original_index": index,
        "valid": True,
        "deliverable": None,
        "checks": {"format": True, "domain": None, "mx": None, "smtp": None},
        "domain_type": "normal",
        "verification_method": "email_timeout",
        "smtp_result": "Verification timed out and will be retried",
        "message": "Verification timed out and will be retried",
        "failure_stage": "smtp",
        "failure_reason": "smtp_timeout",
        "retry_policy": "delayed",
    }
    return ensure_email_risk_signals(result)


def run_verification_worker(
    process_id,
    email_queue,
    result_queue,
    progress_queue,
    shared_domain_cache=None,
    shared_domain_lock=None,
    verifier_factory=None,
):
    """Consume verification work without coordinating random domain probes.

    The shared-cache arguments remain accepted for compatibility with older
    process targets, but are intentionally ignored.
    """
    del shared_domain_cache, shared_domain_lock
    verifier = verifier_factory() if verifier_factory else None
    if verifier is None:
        raise ValueError("verifier_factory is required")

    processed_count = 0
    dns_cache_hits = 0
    consumer_fix_count = 0
    while True:
        try:
            email_data = email_queue.get(timeout=5)
            if email_data is None:
                break

            email, index = email_data
            domain = email.split("@", 1)[1].lower()
            is_consumer_fix = verifier.is_consumer_fix_supported(domain)
            if is_consumer_fix:
                consumer_fix_count += 1

            cache_before = len(verifier.dns_cache)
            progress_queue.put({
                "process_id": process_id,
                "processed": processed_count,
                "current_email": email,
                "status": "processing",
                "is_consumer_fix": is_consumer_fix,
            })
            # This only warms a bounded domain cache while SMTP work is in
            # flight. It never waits on the public provider.
            prefetch_disposable_provider(email)

            try:
                verification_started = time.monotonic()
                with email_verification_deadline(settings.email_hard_timeout_seconds):
                    result = verifier.verify_email_comprehensive(email, process_id)
            except EmailVerificationTimeout:
                result = timeout_result(email, index)
            timings = result.get("timings_ms")
            if not isinstance(timings, dict):
                timings = {}
                result["timings_ms"] = timings
            timings["worker_total"] = round((time.monotonic() - verification_started) * 1000, 2)
            result["original_index"] = index
            ensure_email_risk_signals(result)
            enrich_disposable_provider(result, allow_network=False)
            cache_after = len(verifier.dns_cache)
            if cache_after == cache_before and f"mx_{domain}" in verifier.dns_cache:
                dns_cache_hits += 1
                result["dns_cached"] = True
            else:
                result["dns_cached"] = False

            result_queue.put(result)
            processed_count += 1
            if is_consumer_fix:
                fix_strategy = verifier.get_consumer_fix_strategy(domain)
                time.sleep(fix_strategy["mx_delay"] * 0.3 if fix_strategy else 0.2)
            else:
                strategy_delays = {
                    "fast": 0.1,
                    "normal": 0.2,
                    "medium": 0.3,
                    "strict": 0.5,
                    "super_aggressive": 0.3,
                }
                time.sleep(strategy_delays.get(result.get("strategy", "normal"), 0.2))
        except Exception as error:
            if email_queue.empty():
                break
            progress_queue.put({
                "process_id": process_id,
                "error": str(error),
                "status": "error",
            })

    progress_queue.put({
        "process_id": process_id,
        "processed": processed_count,
        "consumer_fix_count": consumer_fix_count,
        "dns_cache_hits": dns_cache_hits,
        "dns_cache_size": len(verifier.dns_cache),
        "status": "completed",
    })
