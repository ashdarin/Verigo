"""Regression check for concurrent result-export isolation."""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.db.jobs import Job
from app.tasks.verification import write_csv


results_dir = Path(tempfile.mkdtemp(prefix="verigo-result-exports-"))
previous_results_dir = settings.results_dir
object.__setattr__(settings, "results_dir", results_dir)
try:
    finished_at = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)
    first = Job(
        id="export-first", emails=["first@example.com"], worker_count=1,
        status="completed", finished_at=finished_at,
        results=[{"email": "first@example.com", "deliverable": True}],
    )
    second = Job(
        id="export-second", emails=["second@example.com"], worker_count=1,
        status="completed", finished_at=finished_at,
        results=[{"email": "second@example.com", "deliverable": False}],
    )
    write_csv(first)
    write_csv(second)
    assert first.csv_path is not None and second.csv_path is not None
    assert first.csv_path != second.csv_path
    assert "first@example.com" in first.csv_path.read_text(encoding="utf-8-sig")
    assert "second@example.com" in second.csv_path.read_text(encoding="utf-8-sig")
finally:
    object.__setattr__(settings, "results_dir", previous_results_dir)

print("result export uniqueness smoke: ok")
