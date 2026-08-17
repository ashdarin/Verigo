from pathlib import Path


root = Path(__file__).resolve().parents[1]
deploy = root / "deploy"
release = (deploy / "release.sh").read_text(encoding="utf-8")
publish = (deploy / "publish.ps1").read_text(encoding="utf-8")

assert "VERIGO_DEPLOY_ROLE must be" in release
assert "shanghai-app|hong-kong-edge-worker" in release
assert "verigo-apply-release $Role $ReleaseRoot $maintenanceValue" in publish
assert '[ValidateSet("shanghai-app", "hong-kong-edge-worker")]' in publish
assert "status --porcelain" in publish
assert "clean Git working tree" in publish
release_wrapper = (deploy / "verigo-apply-release").read_text(encoding="utf-8")
assert "shanghai-app|hong-kong-edge-worker" in release_wrapper
assert "/tmp/verigo-release" in release_wrapper
assert "VERIGO_DEPLOY_ROLE" in release_wrapper
assert "VERIGO_MONITOR_PROVIDER_PRESSURE_LAST_60_SECONDS" in (
    deploy / "verigo-monitor.env.example"
).read_text(encoding="utf-8")
monitor = (deploy / "verigo-monitor.sh").read_text(encoding="utf-8")
assert "VERIGO_MONITOR_READINESS_URL" in monitor
assert "VERIGO_MONITOR_POSTGRES_TUNNEL_UNIT" in monitor
assert "verigo-monitor-probe/results" in monitor
assert "VERIGO_MONITOR_ASSET_MARKER" in monitor
assert "VERIGO_MONITOR_DATABASE_ENV_FILE" in monitor
assert "queue_health_alerts.py" in monitor
for value in (
    "VERIGO_MONITOR_QUEUE_OLDEST_SECONDS",
    "VERIGO_MONITOR_RUNNING_WITHOUT_LEASE_SECONDS",
    "VERIGO_MONITOR_WORKER_HEARTBEAT_SECONDS",
    "VERIGO_MONITOR_PROVIDER_COOLDOWN_MAX_SECONDS",
):
    assert value in (deploy / "verigo-monitor.env.example").read_text(encoding="utf-8")
assert "OnUnitActiveSec=1m" in (deploy / "verigo-monitor.timer").read_text(encoding="utf-8")

assert "disable_units caddy verigo-monitor.timer" in release
assert "verigo-company-finder-tunnel" in release
assert "verigo-backup.timer verigo-retention.timer" in release
assert "disable_units verigo verigo-worker-api" in release
assert "VERIGO_CLOUDSHELL_LIFECYCLE_DISPATCH_ENABLED=false" in release
assert "VERIGO_CLOUDSHELL_LIFECYCLE_DISPATCH_ENABLED=true" in release
assert "verigo-data-app-tunnel" in release
assert "verigo-postgres-worker-tunnel" in release
assert "verigo-monitor.timer" in release
assert "verigo-qq-worker" in release
assert "disable_units verigo-cloudstudio-keepalive" in release
assert "rm -f /etc/systemd/system/verigo-cloudstudio-keepalive.service" in release
assert "edge_worker_units()" in release
assert "restart_edge_workers()" in release
assert "assert_edge_worker_release()" in release
assert "systemctl list-unit-files --type=service" in release
assert "systemctl list-units --type=service --state=active" in release
assert 'restart_edge_workers || true' in release
assert 'restart_edge_workers\n' in release
assert 'assert_edge_worker_release\n' in release
assert 'verigo-supervisor "${edge_workers[@]}"' in release
assert "managed_cross_route_settings()" in release
assert "sync_managed_cross_route_config /etc/verigo/verigo.env" in release
assert "assert_managed_cross_route_config /etc/verigo/verigo-worker.env" in release
assert "verify_runtime_cross_route_config /etc/verigo/verigo-worker.env" in release
assert "VERIGO_SMTP_CROSS_ROUTE_ENABLED=true" in release
assert "VERIGO_SMTP_CROSS_ROUTE_SHADOW_MODE=false" in release

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

for unit in ("company-vitality-sampler.service", "company-vitality-sampler.timer"):
    assert (deploy / unit).is_file(), unit
assert "OnUnitActiveSec=15min" in (
    deploy / "company-vitality-sampler.timer"
).read_text(encoding="utf-8")
assert "CPUQuota=20%" in (
    deploy / "company-vitality-sampler.service"
).read_text(encoding="utf-8")

worker_unit = (deploy / "verigo-worker@.service").read_text(encoding="utf-8")
supervisor_unit = (deploy / "verigo-supervisor.service").read_text(encoding="utf-8")
for text in (worker_unit, supervisor_unit):
    assert "EnvironmentFile=/etc/verigo/verigo-worker.env" in text
    assert "verigo-postgres-worker-tunnel.service" in text

print("deploy role smoke passed")
