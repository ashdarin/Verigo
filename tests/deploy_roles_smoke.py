from pathlib import Path


root = Path(__file__).resolve().parents[1]
deploy = root / "deploy"
release = (deploy / "release.sh").read_text(encoding="utf-8")
publish = (deploy / "publish.ps1").read_text(encoding="utf-8")

assert "VERIGO_DEPLOY_ROLE must be" in release
assert "shanghai-app|hong-kong-edge-worker" in release
assert "VERIGO_DEPLOY_ROLE=$Role" in publish
assert '[ValidateSet("shanghai-app", "hong-kong-edge-worker")]' in publish
assert "status --porcelain" in publish
assert "clean Git working tree" in publish
assert "VERIGO_MONITOR_PROVIDER_PRESSURE_LAST_60_SECONDS" in (
    deploy / "verigo-monitor.env.example"
).read_text(encoding="utf-8")
assert "OnUnitActiveSec=1m" in (deploy / "verigo-monitor.timer").read_text(encoding="utf-8")

assert "disable_units caddy verigo-monitor.timer" in release
assert "verigo-company-finder-tunnel" in release
assert "verigo-backup.timer verigo-retention.timer" in release
assert "disable_units verigo verigo-worker-api" in release
assert "verigo-data-app-tunnel" in release
assert "verigo-postgres-worker-tunnel" in release
assert "verigo-monitor.timer" in release
assert "verigo-qq-worker" in release
assert "disable_units verigo-cloudstudio-keepalive" in release
assert "rm -f /etc/systemd/system/verigo-cloudstudio-keepalive.service" in release

edge_caddy = (deploy / "Caddyfile.edge").read_text(encoding="utf-8")
shanghai_caddy = (deploy / "Caddyfile.shanghai").read_text(encoding="utf-8")
assert "127.0.0.1:18000" in edge_caddy
assert "127.0.0.1:18001" in edge_caddy
assert "127.0.0.1:8000" in shanghai_caddy
assert "127.0.0.1:8001" in shanghai_caddy

for unit in (
    "verigo-worker-api.service",
    "verigo-company-finder-tunnel.service",
    "verigo-data-app-tunnel.service",
    "verigo-postgres-worker-tunnel.service",
    "verigo-qq-worker.service",
):
    assert (deploy / unit).is_file(), unit

worker_unit = (deploy / "verigo-worker@.service").read_text(encoding="utf-8")
supervisor_unit = (deploy / "verigo-supervisor.service").read_text(encoding="utf-8")
for text in (worker_unit, supervisor_unit):
    assert "EnvironmentFile=/etc/verigo/verigo-worker.env" in text
    assert "verigo-postgres-worker-tunnel.service" in text

print("deploy role smoke passed")
