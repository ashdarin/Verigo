"""Protect the CSV and summary behavior of the extracted reporting module."""

from __future__ import annotations

import csv
import os
import sys
import tempfile
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.verification_reporting import VerificationReporter


results = [
    {
        "email": "person@example.com",
        "timestamp": "2026-08-08T00:00:00",
        "valid": True,
        "deliverable": True,
        "strategy": "normal",
        "smtp_result": "250",
        "message": "ok",
        "process_id": 1,
        "dns_cached": False,
        "checks": {"format": True, "domain": True, "mx": True, "smtp": True},
    }
]

reporter = VerificationReporter(results)
original_cwd = os.getcwd()
with tempfile.TemporaryDirectory(prefix="verigo-report-") as directory:
    os.chdir(directory)
    try:
        output = reporter.export_to_csv("result.csv")
        assert output == "result.csv"
        with open(output, newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.reader(handle))
        assert len(rows) == 2
        assert reporter.get_summary_text()
        reporter.print_summary()
    finally:
        os.chdir(original_cwd)

print("verification reporting smoke: ok")
