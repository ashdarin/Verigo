from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="verigo-result-reuse-"))
os.environ["VERIGO_DATABASE_PATH"] = str(TMP / "verigo.db")
os.environ["VERIGO_RESULTS_DIR"] = str(TMP / "results")
os.environ["VERIGO_SECURE_COOKIES"] = "false"

from app.db.result_objects import ResultObjectStore


store = ResultObjectStore()
owner = "result-reuse-smoke"
first = store.ensure_result(owner, "task-a", 0, {"email": "reuse@example.com", "deliverable": True}, "single")
assert first["status"] == "deliverable"
saved_list = store.create_list(owner, "Smoke list")
assert store.add_results(owner, saved_list["id"], [first["id"]])["added"] == 1
assert store.add_results(owner, saved_list["id"], [first["id"]])["added"] == 0
second = store.ensure_result(owner, "task-b", 0, {"email": "reuse@example.com", "deliverable": False}, "reverify")
assert second["supersedes_result_id"] == first["id"]
assert store.list_results(owner, saved_list["id"], status="deliverable")[0] == 1
assert store.list_results(owner, saved_list["id"], status="invalid")[0] == 0
print("result reuse smoke: ok")
