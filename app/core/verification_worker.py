"""Worker-process implementation for distributed email verification."""

from __future__ import annotations

import time


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

            result = verifier.verify_email_comprehensive(email, process_id)
            result["original_index"] = index
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
