from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.tencent_qq_worker as worker


class StubClient:
    def __init__(self, outcomes) -> None:
        self.outcomes = iter(outcomes)
        self.calls: list[tuple[str, dict[str, object] | None]] = []
        self.closed = False

    def post(self, path: str, *, json=None):
        self.calls.append((path, json))
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def close(self) -> None:
        self.closed = True


def response(status: int, *, payload=None, text: str = "") -> httpx.Response:
    request = httpx.Request("POST", f"https://verigo.site/status-{status}")
    if payload is not None:
        return httpx.Response(status, json=payload, request=request)
    return httpx.Response(status, text=text, request=request)


client = StubClient((
    httpx.ConnectError("connection refused"),
    response(200, payload={"job": None}),
))
with patch.object(worker, "_request_client", return_value=client):
    with patch.object(worker.time, "sleep") as sleep:
        assert worker.request_json("/api/workers/gmail/claim") == {"job": None}
        assert len(client.calls) == 2
        sleep.assert_called_once_with(1)
        assert client.calls[-1] == ("/api/workers/gmail/claim", None)

persistent_client = StubClient(
    httpx.ConnectError("connection refused")
    for _ in range(worker.WORKER_REQUEST_ATTEMPTS)
)
with patch.object(worker, "_request_client", return_value=persistent_client):
    with patch.object(worker.time, "sleep"):
        try:
            worker.request_json("/api/workers/gmail/claim")
        except worker.WorkerRequestError as error:
            assert error.retryable is True
        else:
            raise AssertionError("persistent connection failures must raise")
        assert len(persistent_client.calls) == worker.WORKER_REQUEST_ATTEMPTS

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

bad_gateway = StubClient(
    response(502, text="temporary upstream failure")
    for _ in range(worker.WORKER_REQUEST_ATTEMPTS)
)
with patch.object(worker, "_request_client", return_value=bad_gateway):
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


first_pool = StubClient(())
second_pool = StubClient(())
with patch.object(worker, "_REQUEST_CLIENT", None):
    with patch.object(worker, "_REQUEST_CLIENT_PID", 0):
        with patch.object(worker.os, "getpid", side_effect=(101, 101, 202)):
            with patch.object(worker.httpx, "Client", side_effect=(first_pool, second_pool)) as factory:
                assert worker._request_client() is first_pool
                assert worker._request_client() is first_pool
                assert worker._request_client() is second_pool
assert factory.call_count == 2
assert first_pool.closed is True
assert second_pool.closed is False

inherited_pool = StubClient(())
inherited_lock = worker.threading.Lock()
with patch.object(worker, "_REQUEST_CLIENT", inherited_pool):
    with patch.object(worker, "_REQUEST_CLIENT_PID", 303):
        with patch.object(worker, "_REQUEST_CLIENT_LOCK", inherited_lock):
            worker._reset_request_client_after_fork()
            assert worker._REQUEST_CLIENT is None
            assert worker._REQUEST_CLIENT_PID == 0
            assert worker._REQUEST_CLIENT_LOCK is not inherited_lock
            assert inherited_pool.closed is False


class OutOfOrderVerifier:
    process_count = 0

    def verify_batch_distributed(
        self, emails, *, num_processes, result_callback, should_stop,
    ):
        self.process_count = num_processes
        result_callback({
            "email": "stale-second@qq.com",
            "original_index": 1,
            "deliverable": True,
        })
        result_callback({
            "email": "stale-first@qq.com",
            "original_index": 0,
            "deliverable": False,
        })


qq_verifier = OutOfOrderVerifier()
qq_job = {
    "id": "qq-parallel-mapping",
    "lease_id": "qq-parallel-lease",
    "worker_count": 1,
    "items": [
        {"email": "first@qq.com", "original_index": 41},
        {"email": "second@qq.com", "original_index": 9},
        {"email": "third@qq.com", "original_index": 18},
        {"email": "fourth@qq.com", "original_index": 27},
        {"email": "fifth@qq.com", "original_index": 36},
        {"email": "sixth@qq.com", "original_index": 45},
    ],
}
original_limit = worker.settings.qq_worker_max_workers
object.__setattr__(worker.settings, "qq_worker_max_workers", 6)
try:
    with patch.object(worker, "create_verifier", return_value=qq_verifier):
        with patch.object(worker, "report_results", return_value={}) as report:
            with patch.object(worker, "complete_job"):
                worker._verify_job(
                    qq_job,
                    {"checked_at": worker.time.monotonic(), "stopped": False,
                     "lease_id": "qq-parallel-lease"},
                )
finally:
    object.__setattr__(worker.settings, "qq_worker_max_workers", original_limit)

assert qq_verifier.process_count == 6
reported = report.call_args.args[1]
assert [(item["email"], item["original_index"]) for item in reported] == [
    ("second@qq.com", 9),
    ("first@qq.com", 41),
]

print("remote worker smoke: ok")
