"""Static PostgreSQL schema and release-order contracts for the cache rollout."""
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
schema = (ROOT / "app/db/postgres_schema.py").read_text(encoding="utf-8")
migration = (ROOT / "scripts/ensure_verification_cache_schema.py").read_text(encoding="utf-8")
release = (ROOT / "deploy/release.sh").read_text(encoding="utf-8")
retention = (ROOT / "app/db/retention.py").read_text(encoding="utf-8")

for table in (
    "verification_probe_leases", "verification_probe_waiters", "verification_cache_days",
):
    assert f'TABLES["{table}"]' in schema
    assert table in migration

assert "COALESCE(result_json->>'cache_hit', 'false') <> 'true'" in migration
assert "failure_reason'='smtp_permanent'" in migration
assert "'microsoft_api', 'outlook 账号验证'" in migration
assert "strategy', ''))='outlook_http'" in migration
assert "ensure_verification_cache_schema.py" in release
assert release.index("ensure_verification_cache_schema.py") < release.rindex("systemctl restart")
assert "COALESCE(stale_expires_at, expires_at)" in retention

import scripts.ensure_verification_cache_schema as migration_module  # noqa: E402


class FakeCursor:
    def __init__(self, marker):
        self.marker = marker
        self.statements: list[str] = []
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, _parameters=None):
        self.statements.append(" ".join(str(statement).split()))

    def fetchone(self):
        return self.marker


class FakeConnection:
    def __init__(self, marker):
        self.fake_cursor = FakeCursor(marker)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self.fake_cursor


def run_migration(marker):
    connection = FakeConnection(marker)
    original_connect = migration_module.connect
    original_resolve = migration_module.resolve_database_url
    migration_module.connect = lambda *_args, **_kwargs: connection
    migration_module.resolve_database_url = lambda: "postgresql://fixture"
    try:
        assert migration_module.main() == 0
    finally:
        migration_module.connect = original_connect
        migration_module.resolve_database_url = original_resolve
    return "\n".join(connection.fake_cursor.statements)


skipped_sql = run_migration({"value": migration_module.MIGRATION_VERSION})
assert "WITH latest AS" not in skipped_sql
backfill_sql = run_migration(None)
assert "WITH latest AS" in backfill_sql
assert "INSERT INTO service_state" in backfill_sql
print("verification cache schema smoke: ok")
