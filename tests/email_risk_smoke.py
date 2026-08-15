from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.email_risk import detect_secure_email_gateway, ensure_email_risk_signals, initial_email_risk_signals
import app.tasks.verification as verification_tasks
from app.tasks.verification import normalize_result


signals = initial_email_risk_signals("sales+west@example.com")
assert signals["role_address"]["detected"] is True
assert signals["tagged_address"]["detected"] is True
assert signals["free_provider"]["detected"] is False

signals = initial_email_risk_signals("noreply@gmail.com")
assert signals["free_provider"]["detected"] is True
assert signals["do_not_reply"]["detected"] is True

signals = initial_email_risk_signals("aaaaaaa@yopmail.com")
assert signals["disposable_provider"]["detected"] is True
assert signals["irregular_pattern"]["detected"] is True

signals = initial_email_risk_signals("name" + chr(0x03B1) + "@example.com")
assert signals["unicode_or_suspicious_characters"]["detected"] is True

seg = detect_secure_email_gateway(["mx1-us1.ppe-hosted.com", "us-smtp-inbound-1.mimecast.com"])
assert seg["detected"] is True
assert seg["providers"] == ["mimecast", "proofpoint"]

result = ensure_email_risk_signals({
    "email": "support@company.test",
    "checks": {"format": True},
    "mx_records": ["d123.ess.barracudanetworks.com"],
    "smtp_result": "552 5.2.2 Mailbox over quota",
})
assert result["checks"]["role_address"] is True
assert result["checks"]["mailbox_full"] is True
assert result["checks"]["secure_email_gateway"] is True
assert result["risk_signals"]["secure_email_gateway"]["providers"] == ["barracuda"]

# Remote workers and older cached results receive the same fields during API
# serialization, including mailbox-full messages that were translated for UI.
normalized = normalize_result({
    "email": "no-reply+batch@gmail.com",
    "smtp_result": "552 5.2.2 Mailbox over quota",
    "checks": {"format": True, "domain": True, "mx": True, "smtp": False},
})
assert normalized["risk_signals"]["free_provider"]["detected"] is True
assert normalized["risk_signals"]["do_not_reply"]["detected"] is True
assert normalized["risk_signals"]["tagged_address"]["detected"] is True
assert normalized["risk_signals"]["mailbox_full"]["detected"] is True

# Normalization is also the server-side enrichment point for results returned
# by Cloud Shell and Cloud Studio. It may warm a domain, but never blocks on
# a public lookup while serializing an API response.
prefetched: list[object] = []
network_flags: list[bool] = []
verification_tasks.prefetch_disposable_provider = prefetched.append
original_enrich = verification_tasks.enrich_disposable_provider


def record_enrichment(result, *, allow_network=True):
    network_flags.append(allow_network)
    return original_enrich(result, allow_network=allow_network)


verification_tasks.enrich_disposable_provider = record_enrichment
normalize_result({"email": "remote-worker@unknown.example", "smtp_result": "250 2.1.5 Recipient OK"})
assert prefetched == ["remote-worker@unknown.example"]
assert network_flags == [False]

print("email risk smoke: ok")
