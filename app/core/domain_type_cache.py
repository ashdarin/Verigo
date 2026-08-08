"""Compatibility hooks for the removed random catch-all probe cache.

The verifier no longer classifies domains with synthetic recipients. These
functions remain as no-ops so older CLI integrations can keep importing the
historical names while deployments age out the cache file naturally.
"""


def load_persistent_cache() -> None:
    """Keep the legacy startup hook without loading obsolete probe evidence."""


def save_persistent_cache() -> None:
    """Keep the legacy shutdown hook without writing probe evidence."""


def get_shared_domain_type(_domain: str):
    return None


def set_shared_domain_type(_domain: str, _domain_type: str, *, probe_codes=None) -> None:
    del probe_codes


def has_catch_all_evidence(_cache_entry) -> bool:
    return False
