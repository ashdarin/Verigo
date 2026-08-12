# Cloud Studio Worker Rebuild

## Decision

Cloud Studio is treated as a managed workspace, not as a general remote-command host.
The production control plane uses only the public Tencent Cloud Studio API 3.0 actions:

- `ModifyWorkspace` to install the environment and Start lifecycle command.
- `DescribeWorkspaces` to read workspace state.
- `RunWorkspace` and `StopWorkspace` to manage capacity.
- `CreateWorkspaceToken` only in the separate session service when an IDE session is required by Cloud Studio to keep the workspace active.

The queue supervisor no longer creates temporary tokens, opens IDE sessions, uses SSH,
or executes worker commands. The API does not expose a remote command execution action.

References:

- Tencent Cloud Studio documentation: https://cloud.tencent.com/document/product/1781
- Cloud Studio API endpoint used by the official SDK: `cloudstudio.tencentcloudapi.com`
- API version: `2023-05-08`

## Runtime Ownership

1. Deployment publishes an authenticated worker bundle.
2. `configure_cloudstudio_worker.py` writes a deterministic Start lifecycle command.
3. The session service keeps the managed workspace session active.
4. The Start hook downloads the bundle and starts fixed worker processes directly.
5. Workers claim leases, send an immediate heartbeat, then heartbeat every 10 seconds.
6. The production supervisor reclaims any lease that exceeds the lease window.

## Removed Paths

- Temporary-token SSH bootstrap.
- SSH key and known-host checks in queue supervision.
- Playwright execution inside the queue supervisor.
- Nested shell watchdog processes and layout marker files.
- Startup timeout conversion of unfinished addresses into final failures.

## Deployment Sequence

1. Keep `VERIGO_TENCENT_QQ_WORKER_ENABLED=false`.
2. Deploy the rebuilt control plane and worker bundle.
3. Apply the lifecycle configuration with `configure_cloudstudio_worker.py`.
4. Stop and start the workspace once.
5. Confirm bundle download returns HTTP 200.
6. Enable QQ worker API access for one controlled job.
7. Require an immediate heartbeat, another heartbeat after 10 seconds, and a durable result.
8. Observe at least ten completed addresses without a stale lease.
9. Resume the preserved QQ queues.

## Acceptance Criteria

- Main API and PostgreSQL health remain `ok`.
- Worker IDs remain stable across workspace restarts.
- Every claimed lease receives a heartbeat within 5 seconds.
- Heartbeats continue at intervals no longer than 15 seconds during SMTP work.
- Result or timeout-result callbacks close the lease.
- Killing a worker returns its unfinished address to `pending` after lease expiry.
- A release does not require SSH access to either workspace.
- Completed results are never reset during restart or recovery.

## Rollback

Disable `VERIGO_TENCENT_QQ_WORKER_ENABLED`, restart Worker API and Supervisor, and leave
the QQ jobs queued. Completed result rows remain intact. Restore the previous worker
bundle only after the queue is closed to remote claims.
