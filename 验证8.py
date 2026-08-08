#!/usr/bin/env python3
"""Compatibility entry point for the legacy verifier.

The implementation lives in :mod:`app.core.legacy_verifier` so application
code has a stable package-owned module while existing scripts can continue to
import the historical filename.
"""

# These module objects are intentionally re-exported. Older integrations and
# smoke tests patch their network/time dependencies through this compatibility
# module; both sides therefore observe the same objects.
import dns
import smtplib
import time

from app.core.legacy_verifier import *  # noqa: F401,F403
from app.core.legacy_verifier import main as _legacy_main


if __name__ == "__main__":
    _legacy_main()
