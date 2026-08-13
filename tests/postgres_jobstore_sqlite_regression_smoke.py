"""Ensure JobStore dual-backend changes still work on SQLite (default path)."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    # Force sqlite mode and isolated DB.
    for key in list(os.environ):
        if key.startswith("VERIGO_"):
            os.environ.pop(key, None)
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "t.db"
        os.environ["VERIGO_DATABASE_PATH"] = str(db)
        os.environ["VERIGO_POSTGRES_ENABLED"] = "false"
        os.environ["VERIGO_ALLOW_SQLITE"] = "true"
        os.environ.pop("VERIGO_DATABASE_URL", None)
        import importlib
        import app.config as config

        importlib.reload(config)
        import app.db.jobs as jobs_mod

        importlib.reload(jobs_mod)
        store = jobs_mod.JobStore()
        store.initialize()
        store.set_service_mode("draining")
        assert store.service_mode() == "draining"
        store.set_service_mode("active")
        assert store.service_mode() == "active"
        summary = store.health_summary()
        assert summary["service_mode"] == "active"
        assert summary["queued_jobs"] == 0
        claimed = store.claim_next(worker_id="w1", execution_target="local")
        assert claimed is None
        print("OK  sqlite jobstore service_mode/health/claim_next")
    # restore clean config for other importers
    for key in list(os.environ):
        if key.startswith("VERIGO_"):
            os.environ.pop(key, None)
    import importlib
    import app.config as config

    importlib.reload(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
