"""Worker-process implementation for distributed email verification."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from app.core.domain_type_cache import has_catch_all_evidence


def run_verification_worker(
    process_id,
    email_queue,
    result_queue,
    progress_queue,
    shared_domain_cache,
    shared_domain_lock,
    verifier_factory,
):
    """Consume one verification queue and report progress without owning orchestration."""
    try:
        verifier = verifier_factory()
        if shared_domain_cache:
            try:
                for domain, cache_data in dict(shared_domain_cache).items():
                    verifier.domain_type_cache[domain] = cache_data
            except Exception:
                pass

        original_detect = verifier.detect_catch_all_domain

        def detect_with_shared_cache(domain):
            if domain in verifier.domain_type_cache:
                cache_entry = verifier.domain_type_cache[domain]
                if (
                    datetime.now() - cache_entry["checked_at"] < timedelta(hours=1)
                    and has_catch_all_evidence(cache_entry)
                ):
                    return cache_entry["type"]

            is_probe_owner = False
            if shared_domain_cache and shared_domain_lock:
                try:
                    with shared_domain_lock:
                        cache_entry = shared_domain_cache.get(domain)
                        if cache_entry and cache_entry.get("type") != "probing":
                            if (
                                datetime.now() - cache_entry["checked_at"] < timedelta(hours=1)
                                and has_catch_all_evidence(cache_entry)
                            ):
                                verifier.domain_type_cache[domain] = cache_entry
                                return cache_entry["type"]
                        if not cache_entry or cache_entry.get("type") != "probing":
                            shared_domain_cache[domain] = {
                                "type": "probing",
                                "checked_at": datetime.now(),
                            }
                            is_probe_owner = True
                except Exception:
                    pass

            if shared_domain_cache and not is_probe_owner:
                for _ in range(20):
                    time.sleep(0.25)
                    try:
                        cache_entry = shared_domain_cache.get(domain)
                        if (
                            cache_entry
                            and cache_entry.get("type") != "probing"
                            and has_catch_all_evidence(cache_entry)
                        ):
                            verifier.domain_type_cache[domain] = cache_entry
                            return cache_entry["type"]
                    except Exception:
                        break

            result = original_detect(domain)
            if shared_domain_cache and domain in verifier.domain_type_cache:
                try:
                    shared_domain_cache[domain] = verifier.domain_type_cache[domain]
                except Exception:
                    pass
            return result

        verifier.detect_catch_all_domain = detect_with_shared_cache

        processed_count = 0
        dns_cache_hits = 0
        consumer_fix_count = 0

        while True:
            try:
                email_data = email_queue.get(timeout=5)
                if email_data is None:
                    break

                email, index = email_data
                domain = email.split("@")[1].lower()
                is_consumer_fix = verifier.is_consumer_fix_supported(domain)
                if is_consumer_fix:
                    consumer_fix_count += 1

                cache_before = len(verifier.dns_cache)
                progress_queue.put(
                    {
                        "process_id": process_id,
                        "processed": processed_count,
                        "current_email": email,
                        "status": "processing",
                        "is_consumer_fix": is_consumer_fix,
                    }
                )

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
                progress_queue.put(
                    {
                        "process_id": process_id,
                        "error": str(error),
                        "status": "error",
                    }
                )

        progress_queue.put(
            {
                "process_id": process_id,
                "processed": processed_count,
                "consumer_fix_count": consumer_fix_count,
                "dns_cache_hits": dns_cache_hits,
                "dns_cache_size": len(verifier.dns_cache),
                "status": "completed",
            }
        )
    except Exception as error:
        progress_queue.put(
            {
                "process_id": process_id,
                "error": str(error),
                "status": "failed",
            }
        )
