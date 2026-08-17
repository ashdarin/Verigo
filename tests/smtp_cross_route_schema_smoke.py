from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
migration = (ROOT / "scripts/ensure_smtp_cross_route_schema.py").read_text(encoding="utf-8")
schema = (ROOT / "app/db/postgres_schema.py").read_text(encoding="utf-8")
release = (ROOT / "deploy/release.sh").read_text(encoding="utf-8")
events = (ROOT / "app/db/smtp_review_events.py").read_text(encoding="utf-8")

assert 'TABLES["smtp_review_events"]' in schema
assert "email_hash" in schema
assert 'name="email"' not in schema[schema.index('TABLES["smtp_review_events"]'):schema.index('TABLES["jobs"]')]
assert "CREATE TABLE IF NOT EXISTS smtp_review_events" in migration
assert "idx_smtp_review_events_type_time" in migration
assert "idx_smtp_review_events_provider_time" in migration
assert "ensure_smtp_cross_route_schema.py" in release
assert release.index("ensure_smtp_cross_route_schema.py") < release.rindex("systemctl restart")
assert "hmac.new" in events
assert "executemany" in events
assert "ON CONFLICT(id) DO NOTHING" in events

print("smtp cross-route schema smoke: ok")
