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
if not os.environ.get("VERIGO_DATABASE_URL", "").strip():
    print(
        "SKIP result_reuse_smoke: SQLite is no longer an application backend. "
        "Set VERIGO_DATABASE_URL to run this smoke against PostgreSQL."
    )
    raise SystemExit(0)

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

from types import SimpleNamespace

completed = SimpleNamespace(
    id="complete-publish",
    owner_id=owner,
    parent_id=None,
    retry_parent_id=None,
    execution_target="local",
    stop_on_deliverable=False,
    emails=["finish@example.com"],
    results=[{"email": "finish@example.com", "deliverable": True, "original_index": 0}],
)
assert store.publish_completed_job(completed) == 1
assert any(item["task_id"] == "complete-publish" and item["status"] == "deliverable" for item in store.recent_results(owner, 8))
completed.results = [{"email": "finish@example.com", "deliverable": False, "original_index": 0}]
assert store.publish_completed_job(completed) == 1
assert any(item["task_id"] == "complete-publish" and item["status"] == "undeliverable" for item in store.recent_results(owner, 8))
assert store.publish_completed_job(SimpleNamespace(
    id="complete-guest", owner_id=None, parent_id=None, retry_parent_id=None,
    execution_target="local", stop_on_deliverable=False, emails=["guest@example.com"],
    results=[{"email": "guest@example.com", "deliverable": True}],
)) == 0
assert store.publish_completed_job(SimpleNamespace(
    id="complete-child", owner_id=owner, parent_id="parent-id", retry_parent_id=None,
    execution_target="local", stop_on_deliverable=False, emails=["child@example.com"],
    results=[{"email": "child@example.com", "deliverable": True}],
)) == 0

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
