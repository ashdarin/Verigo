# Deployment Roles

Every release must set exactly one `VERIGO_DEPLOY_ROLE`. The publish script
requires the same role and maps it to the expected host by default.

## `shanghai-app`

- Runs `verigo`, `verigo-worker-api`, PostgreSQL, the Company Finder tunnel,
  and backup/retention timers.
- Keeps Caddy, external monitoring, all verification workers, worker database
  tunnels, CloudStudio keepalive, and the QQ worker disabled.
- Publishes the CloudStudio worker bundle from `/opt/verigo/data`.
- Applies the additive verification-cache schema and one-time history backfill
  before restarting Web/API services.

## `hong-kong-edge-worker`

- Runs public Caddy, the application tunnel to Shanghai, external monitoring,
  PostgreSQL worker tunnels, ordinary workers, supervisor, and the
  systemd-managed QQ worker. CloudStudio is strictly on-demand: its old
  keepalive unit is removed during every edge deployment.
- Keeps the Web/API, Company Finder tunnel, and backup/retention disabled.
- Uses `Caddyfile.edge`, whose upstreams are `127.0.0.1:18000` and `:18001`.

Examples:

```powershell
.\deploy\publish.ps1 -Role shanghai-app
.\deploy\publish.ps1 -Role hong-kong-edge-worker
```

The edge role activates a release without restarting already-running workers.
Use `-Maintenance` only when an immediate worker/supervisor restart is intended.
Deploy `shanghai-app` first whenever a release changes shared PostgreSQL schema;
deploy the edge worker role only after the Shanghai health and schema checks pass.
