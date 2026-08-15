from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.core import disposable_lookup
from app.core.email_risk import enrich_disposable_provider


class FakeStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def service_state_value(self, name: str) -> str | None:
        return self.values.get(name)

    def set_service_state_value(self, name: str, value: str) -> None:
        self.values[name] = value


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self.payload


calls: list[dict[str, object]] = []


def fake_get(_url: str, **kwargs: object) -> FakeResponse:
    calls.append(kwargs)
    return FakeResponse({"disposable": "true"})


object.__setattr__(settings, "disposable_lookup_enabled", True)
object.__setattr__(settings, "disposable_lookup_url", "https://example.test/disposable")
object.__setattr__(settings, "disposable_lookup_timeout_seconds", 0.2)
object.__setattr__(settings, "disposable_lookup_positive_cache_hours", 1)
object.__setattr__(settings, "disposable_lookup_negative_cache_hours", 1)
object.__setattr__(settings, "disposable_lookup_failure_cache_seconds", 10)
disposable_lookup.job_store = FakeStore()
disposable_lookup.httpx.get = fake_get
disposable_lookup.clear_memory_cache_for_tests()

assert disposable_lookup.lookup_disposable_domain("mailinator.com") is True
assert calls == [{"params": {"email": "probe@mailinator.com"}, "timeout": 0.2, "follow_redirects": False}]
assert disposable_lookup.lookup_disposable_domain("mailinator.com") is True
assert len(calls) == 1

# The non-blocking read path never reaches the public provider on a cache miss.
assert disposable_lookup.lookup_disposable_domain("not-cached.example", allow_network=False) is None
assert len(calls) == 1

# Unknown domains are warmed in a bounded background worker. The front-end
# result path can consume its shared-cache entry without making a network call.
disposable_lookup.prefetch_disposable_domain("background.example")
deadline = time.monotonic() + 1
while len(calls) < 2 and time.monotonic() < deadline:
    time.sleep(0.01)
assert len(calls) == 2
assert calls[-1]["params"] == {"email": "probe@background.example"}
disposable_lookup.clear_memory_cache_for_tests()
assert disposable_lookup.lookup_disposable_domain("background.example", allow_network=False) is True
assert len(calls) == 2

# A provider failure is cached as a deliberate unknown result. Prefetch must
# not turn that into repeated requests during the failure-cache window.
failed_calls = 0


def failing_get(_url: str, **_kwargs: object) -> FakeResponse:
    global failed_calls
    failed_calls += 1
    raise httpx.ConnectTimeout("provider unavailable")


disposable_lookup.httpx.get = failing_get
assert disposable_lookup.lookup_disposable_domain("failure.example") is None
assert failed_calls == 1
disposable_lookup.prefetch_disposable_domain("failure.example")
time.sleep(0.05)
assert failed_calls == 1
disposable_lookup.httpx.get = fake_get

# A new worker process has no memory cache, so this proves the durable
# domain-keyed cache prevents another public API call.
disposable_lookup.clear_memory_cache_for_tests()
assert disposable_lookup.lookup_disposable_domain("mailinator.com") is True
assert len(calls) == 2

result = enrich_disposable_provider({"email": "customer-local-part@unknown.example"})
assert result["risk_signals"]["disposable_provider"]["detected"] is True
assert result["risk_signals"]["disposable_provider"]["source"] == "debounce"
assert calls[-1]["params"] == {"email": "probe@unknown.example"}

print("disposable lookup smoke: ok")
