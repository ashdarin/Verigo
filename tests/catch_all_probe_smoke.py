"""Regression check that random catch-all probing stays disabled."""
from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.legacy import load_legacy_module


module = load_legacy_module()
verifier = module.EmailVerifier()
assert verifier.detect_catch_all_domain("example.test") == "normal"
print("catch-all probe disabled smoke: ok")
