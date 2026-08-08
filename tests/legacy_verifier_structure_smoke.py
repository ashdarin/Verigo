"""Keep the historical verifier import path stable during modularization."""

import importlib
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

legacy = importlib.import_module("验证8")
implementation = importlib.import_module("app.core.legacy_verifier")
smtp = importlib.import_module("app.core.smtp_verifier")

assert legacy.EmailVerifier is smtp.EmailVerifier
assert legacy.DistributedEmailVerifier is implementation.DistributedEmailVerifier
assert legacy.load_persistent_cache is implementation.load_persistent_cache
assert legacy.save_persistent_cache is implementation.save_persistent_cache
assert legacy.smtplib is smtp.smtplib
assert not hasattr(implementation, "CatchAllBounceProber")
assert not hasattr(implementation.DistributedEmailVerifier, "_start_catch_all_probes")

print("legacy verifier structure smoke: ok")
