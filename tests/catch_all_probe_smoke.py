"""Regression checks for strict catch-all classification."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.legacy import load_legacy_module


class FakeSMTP:
    responses: list[int] = []
    connections = 0

    def __init__(self, timeout: int) -> None:
        self.timeout = timeout
        type(self).connections += 1

    def connect(self, host: str, port: int) -> tuple[int, bytes]:
        return 220, b"ready"

    def ehlo(self, host: str) -> tuple[int, bytes]:
        return 250, b"hello"

    def mail(self, address: str) -> tuple[int, bytes]:
        return 250, b"sender ok"

    def rcpt(self, address: str) -> tuple[int, bytes]:
        return type(self).responses.pop(0), b"recipient result"

    def quit(self) -> tuple[int, bytes]:
        return 221, b"bye"


module = load_legacy_module()


def classify(domain: str, responses: list[int]) -> str:
    FakeSMTP.responses = list(responses)
    FakeSMTP.connections = 0
    verifier = module.EmailVerifier()
    verifier.get_mx_records = lambda domain: ["mx.example.test"]
    with patch.object(module.smtplib, "SMTP", FakeSMTP):
        result = verifier.detect_catch_all_domain(domain)
    return result


assert classify("catch-all-yes.example", [250, 250, 250]) == "catch-all"
assert FakeSMTP.connections == 3
assert classify("catch-all-mixed.example", [250, 550]) == "normal"
assert FakeSMTP.connections == 2
assert classify("catch-all-no.example", [550]) == "normal"
assert FakeSMTP.connections == 1
print("catch-all probe smoke: ok")
