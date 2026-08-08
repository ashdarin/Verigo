from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.legacy_verifier import DistributedEmailVerifier
from app.core.verification_input import VerificationInput


with tempfile.TemporaryDirectory() as raw_dir:
    directory = Path(raw_dir)
    (directory / "items.txt").write_text(
        " first@example.com\nnot-an-email\nsecond@example.net\n",
        encoding="utf-8",
    )
    (directory / "items.csv").write_text(
        "name,email\nAlice,alice@example.com\n",
        encoding="utf-8",
    )
    (directory / "items.json").write_text(
        json.dumps({"emails": ["json@example.com", "ignored"]}),
        encoding="utf-8",
    )

    assert VerificationInput.load_emails_from_file(directory / "items.txt") == [
        "first@example.com",
        "second@example.net",
    ]
    assert VerificationInput.load_emails_from_file(directory / "items.csv") == [
        "alice@example.com"
    ]
    assert VerificationInput.load_emails_from_file(directory / "items.json") == [
        "json@example.com"
    ]

verifier = DistributedEmailVerifier()
raw = [" First@example.com ", "first@example.com", "bad address@example.com", ""]
expected = ["First@example.com"]
assert verifier._clean_email_list(raw) == expected
assert VerificationInput.clean_email_list(raw) == expected

print("verification input smoke passed")
