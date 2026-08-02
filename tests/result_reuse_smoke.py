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
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

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

# Route-level guard checks: ownership and invalid batch indexes must fail before
# any Result Object is created.
from fastapi.testclient import TestClient
from app.main import app
from app.db.auth import auth_store

with TestClient(app) as client:
    registered = client.post("/api/auth/register", json={"email": "reuse-api@example.com", "password": "reuse-password-2026"})
    assert registered.status_code == 201, registered.text
    user_id = registered.json()["id"]
    code = auth_store.create_email_verification(user_id)
    auth_store.confirm_email_verification(user_id, code)
    target = store.create_list(user_id, "API reuse")
    assert client.post("/api/results/save-batch", json={"job_id": "missing-job", "result_indices": [99], "list_id": target["id"]}).status_code == 404
    assert client.get("/api/lists/not-owned").status_code == 404
print("result reuse smoke: ok")
