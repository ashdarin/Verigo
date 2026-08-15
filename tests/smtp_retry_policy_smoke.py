from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.smtp_retry_policy import apply_retry_plan, retry_plan
from app.core.verification_outcome import (
    RETRY_DELAYED,
    RETRY_GREYLIST,
    RETRY_NEVER,
    ensure_outcome,
    retry_policy,
)


greylist = {"smtp_result": "451 4.7.1 Please try again later: greylisted"}
ensure_outcome(greylist)
assert retry_policy(greylist) == RETRY_GREYLIST
assert greylist["retry_class"] == "greylist"
assert greylist["retry_max_attempts"] == 2
assert retry_plan(greylist).delay_for_attempt(1) == 15 * 60
assert retry_plan(greylist).delay_for_attempt(2) == 60 * 60
assert retry_plan(greylist).delay_for_attempt(3) is None

throttled = {"smtp_result": "421 4.7.0 too many connections; try again later"}
ensure_outcome(throttled)
assert retry_policy(throttled) == RETRY_DELAYED
assert throttled["failure_reason"] == "smtp_receiver_throttled"
assert throttled["retry_class"] == "receiver_throttled"
assert throttled["receiver_cooldown_seconds"] == 20 * 60
assert retry_plan(throttled).delay_for_attempt(1) == 20 * 60

temporary = {"smtp_result": "452 4.3.1 temporary recipient failure"}
ensure_outcome(temporary)
assert retry_policy(temporary) == RETRY_DELAYED
assert temporary["retry_class"] == "temporary"
assert temporary["retry_max_attempts"] == 1
assert retry_plan(temporary).delay_for_attempt(1) == 30 * 60

infrastructure = {"smtp_result": "SMTP connection timed out", "failure_reason": "smtp_timeout"}
ensure_outcome(infrastructure)
assert retry_policy(infrastructure) == RETRY_DELAYED
assert infrastructure["retry_class"] == "infrastructure"
assert retry_plan(infrastructure).delay_for_attempt(1) == 5 * 60

mailbox_full = {"smtp_result": "452 4.2.2 mailbox over quota"}
ensure_outcome(mailbox_full)
assert retry_policy(mailbox_full) == RETRY_NEVER
assert apply_retry_plan(mailbox_full).retry_class == "none"

print("smtp retry policy smoke: ok")
