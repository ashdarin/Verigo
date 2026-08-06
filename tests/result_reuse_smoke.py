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
updated_list = store.update_list(owner, saved_list["id"], name="Updated smoke list")
assert updated_list["name"] == "Updated smoke list"
second = store.ensure_result(owner, "task-b", 0, {"email": "reuse@example.com", "deliverable": False}, "reverify")
assert second["supersedes_result_id"] == first["id"]
assert store.list_results(owner, saved_list["id"], status="deliverable")[0] == 1
assert store.list_results(owner, saved_list["id"], status="invalid")[0] == 0
history = store.result_history(owner, second["id"])
assert history["email"] == "reuse@example.com"
assert [item["id"] for item in history["items"]] == [second["id"], first["id"]]
store.archive_list(owner, saved_list["id"])
assert store.get_list(owner, saved_list["id"]) is None

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
    workspace = client.get("/api/workspace")
    assert workspace.status_code == 200, workspace.text
    assert workspace.json()["total"] == 0
    assert workspace.json()["processed_today"] == 0
    target = store.create_list(user_id, "API reuse")
    updated = client.patch(
        f"/api/lists/{target['id']}",
        json={"name": "API reuse updated", "description": "smoke"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "API reuse updated"
    api_result = store.ensure_result(
        user_id, "api-history", 0,
        {"email": "history@example.com", "deliverable": True}, "single",
    )
    history_response = client.get(f"/api/results/{api_result['id']}/history")
    assert history_response.status_code == 200, history_response.text
    assert history_response.json()["items"][0]["id"] == api_result["id"]
    assert client.delete(f"/api/lists/{target['id']}").status_code == 204
    assert client.get(f"/api/lists/{target['id']}").status_code == 404
    assert client.post("/api/results/save-batch", json={"job_id": "missing-job", "result_indices": [99], "list_id": target["id"]}).status_code == 404
    assert client.post("/api/results/save-batch", json={"job_id": "missing-job", "result_indices": [0], "list_name": ""}).status_code == 422
    assert client.get("/api/lists/not-owned").status_code == 404
print("result reuse smoke: ok")
