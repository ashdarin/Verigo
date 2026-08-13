from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.tencent_qq_worker as worker


responses = iter((
    SimpleNamespace(returncode=7, stderr="curl: (7) connection refused", stdout=""),
    SimpleNamespace(returncode=0, stderr="", stdout='{"job": null}'),
))
with patch.object(worker.subprocess, "run", side_effect=lambda *args, **kwargs: next(responses)) as run:
    with patch.object(worker.time, "sleep") as sleep:
        assert worker.request_json("/api/workers/gmail/claim") == {"job": None}
        assert run.call_count == 2
        sleep.assert_called_once_with(1)
        command = run.call_args.args[0]
        assert "--retry-connrefused" not in command
        assert "--connect-timeout" in command
        assert command[command.index("--max-time") + 1] == "75"

with patch.object(
    worker.subprocess,
    "run",
    return_value=SimpleNamespace(returncode=7, stderr="curl: (7) connection refused", stdout=""),
) as run:
    with patch.object(worker.time, "sleep"):
        try:
            worker.request_json("/api/workers/gmail/claim")
        except worker.WorkerRequestError as error:
            assert error.retryable is True
        else:
            raise AssertionError("persistent connection failures must raise")
        assert run.call_count == worker.WORKER_REQUEST_ATTEMPTS

job = {"id": "lease-heartbeat-smoke", "lease_id": "lease-token", "items": []}
with patch.object(worker, "_verify_job") as verify:
    worker.verify_job(job)
    verify.assert_called_once()

timeout_job = {
    "id": "parent-timeout-smoke",
    "lease_id": "timeout-lease",
    "items": [{"email": "slow@qq.com", "original_index": 17}],
}
with patch.object(
    worker, "_verify_job", side_effect=worker.EmailVerificationTimeout("stalled")
):
    with patch.object(worker, "report_parent_timeout") as report_timeout:
        worker.verify_job(timeout_job)
        report_timeout.assert_called_once_with(timeout_job, "timeout-lease")

with patch.object(
    worker.subprocess,
    "run",
    return_value=SimpleNamespace(
        returncode=22,
        stderr="curl: (22) The requested URL returned error: 502",
        stdout="",
    ),
):
    with patch.object(worker.time, "sleep"):
        try:
            worker.request_json("/api/workers/gmail/jobs/smoke/results")
        except worker.WorkerRequestError as error:
            assert error.retryable is True
        else:
            raise AssertionError("HTTP 502 must be treated as a transient worker outage")

with patch.object(worker, "request_json", return_value={}) as request:
    worker.complete_job("completion-smoke", "lease-token")
    assert request.call_args.args == (
        f"/api/workers/{worker.WORKER_TARGET}/jobs/completion-smoke/complete",
        {"results": [], "lease_id": "lease-token"},
    )

print("remote worker smoke: ok")
