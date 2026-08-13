"""Config conflict guards for PostgreSQL cutover switches."""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _reload_config(env: dict[str, str]):
    for key in list(os.environ):
        if key.startswith("VERIGO_"):
            os.environ.pop(key, None)
    os.environ.update(env)
    import app.config as config

    importlib.reload(config)
    return config


def test_default_sqlite() -> None:
    cfg = _reload_config({})
    assert cfg.settings.postgres_enabled is False


def test_enabled_requires_dsn() -> None:
    try:
        _reload_config({"VERIGO_POSTGRES_ENABLED": "true"})
    except RuntimeError as exc:
        assert "VERIGO_DATABASE_URL" in str(exc)
        return
    raise AssertionError("expected RuntimeError when DSN missing")


def test_store_flag_conflict() -> None:
    try:
        _reload_config(
            {
                "VERIGO_POSTGRES_ENABLED": "true",
                "VERIGO_DATABASE_URL": "postgresql://u:p@127.0.0.1:15432/db",
                "VERIGO_AUTH_POSTGRES_ENABLED": "false",
            }
        )
    except RuntimeError as exc:
        assert "VERIGO_AUTH_POSTGRES_ENABLED" in str(exc)
        return
    raise AssertionError("expected store-flag conflict")


def test_enabled_ok() -> None:
    cfg = _reload_config(
        {
            "VERIGO_POSTGRES_ENABLED": "true",
            "VERIGO_DATABASE_URL": "postgresql://u:p@127.0.0.1:15432/db",
        }
    )
    assert cfg.settings.postgres_enabled is True
    assert "15432" in cfg.settings.database_url


def main() -> int:
    # Restore a clean default after tests so other importers are not poisoned.
    tests = [
        test_default_sqlite,
        test_enabled_requires_dsn,
        test_store_flag_conflict,
        test_enabled_ok,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"OK  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    _reload_config({})
    if failed:
        return 1
    print("all postgres config guard smoke tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
