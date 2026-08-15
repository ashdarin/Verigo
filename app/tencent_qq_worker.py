from __future__ import annotations

import os
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import settings
from app.core.legacy import create_verifier
from app.core.provider_policy import is_qq_email
from app.core.prospecting_protection import is_suspicious_recipient_rejection
from app.core.result_retry import (
    is_retryable_smtp_result,
    is_smtp_greylisted,
    smtp_temporary_status,
)
from app.core.verification_worker import (
    EmailVerificationTimeout,
    email_verification_deadline,
    timeout_result,
)


WORKER_TARGET = os.getenv("VERIGO_REMOTE_WORKER_TARGET", "tencent-qq")
SERVER_URL = os.getenv("VERIGO_REMOTE_WORKER_SERVER", os.getenv("VERIGO_TENCENT_QQ_SERVER", "https://verigo.site")).rstrip("/")
TOKEN = os.getenv("VERIGO_REMOTE_WORKER_TOKEN", os.getenv("VERIGO_TENCENT_QQ_WORKER_TOKEN", ""))
WORKER_ID = os.getenv(
    "VERIGO_REMOTE_WORKER_ID",
    os.getenv("VERIGO_TENCENT_QQ_WORKER_ID", f"cloudstudio-{socket.gethostname()}-{os.getpid()}"),
)
POLL_SECONDS = max(0.1, float(os.getenv("VERIGO_TENCENT_QQ_POLL_SECONDS", "0.25")))
RETRY_SECONDS = max(1.0, float(os.getenv("VERIGO_TENCENT_QQ_RETRY_SECONDS", "5")))
WORKER_CAPACITY = max(1, int(os.getenv("VERIGO_REMOTE_WORKER_CAPACITY", "1")))
# A long-poll records the node heartbeat once per wait window instead of once
# per local poll interval. This keeps idle worker fleets from contending on
# ``worker_nodes`` while preserving immediate claims when work is queued.
CLAIM_WAIT_SECONDS = min(
    25, max(1, int(os.getenv("VERIGO_REMOTE_WORKER_CLAIM_WAIT_SECONDS", "20")))
)
PARENT_TIMEOUT_GRACE_SECONDS = 15


class WorkerRequestError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


# Worker API deploys can briefly take the upstream out of rotation while the
# public site stays available. Keep an in-flight lease callback alive through
# that window so results already acknowledged by ``/results`` still receive
# their matching ``/complete`` callback.
WORKER_REQUEST_ATTEMPTS = 8
WORKER_REQUEST_RETRY_MAX_SECONDS = 12
TRANSIENT_HTTP_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_REQUEST_CLIENT: httpx.Client | None = None
_REQUEST_CLIENT_PID = 0
_REQUEST_CLIENT_LOCK = threading.Lock()


def _reset_request_client_after_fork() -> None:
    """Discard inherited HTTP state without touching a parent-owned pool."""
    global _REQUEST_CLIENT, _REQUEST_CLIENT_PID, _REQUEST_CLIENT_LOCK
    _REQUEST_CLIENT = None
    _REQUEST_CLIENT_PID = 0
    _REQUEST_CLIENT_LOCK = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_request_client_after_fork)


def _request_client() -> httpx.Client:
    """Reuse worker API connections without sharing a pool across a fork."""
    global _REQUEST_CLIENT, _REQUEST_CLIENT_PID
    pid = os.getpid()
    with _REQUEST_CLIENT_LOCK:
        if _REQUEST_CLIENT is None or _REQUEST_CLIENT_PID != pid:
            if _REQUEST_CLIENT is not None:
                _REQUEST_CLIENT.close()
            _REQUEST_CLIENT = httpx.Client(
                base_url=SERVER_URL,
                headers={
                    "Content-Type": "application/json",
                    "X-Verigo-Worker-Token": TOKEN,
                    "X-Verigo-Worker-Id": WORKER_ID,
                    "X-Verigo-Worker-Capacity": str(WORKER_CAPACITY),
                },
                timeout=httpx.Timeout(75, connect=10),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            )
            _REQUEST_CLIENT_PID = pid
        return _REQUEST_CLIENT


def request_json(path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    for attempt in range(WORKER_REQUEST_ATTEMPTS):
        try:
            response = _request_client().post(path, json=payload)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("worker API response must be a JSON object")
            return data
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            message = f"worker API returned HTTP {status}"
            # Keep FastAPI's compact validation detail so remote logs identify
            # malformed callbacks without exposing the submitted result body.
            if exc.response.text.strip():
                detail = exc.response.text.strip().replace("\n", " ")[:500]
                message = f"{message}: {detail}"
            retryable = status in TRANSIENT_HTTP_STATUS_CODES
        except (httpx.RequestError, ValueError) as exc:
            retryable = True
            message = str(exc)
        if retryable and attempt + 1 < WORKER_REQUEST_ATTEMPTS:
            time.sleep(min(2 ** attempt, WORKER_REQUEST_RETRY_MAX_SECONDS))
            continue
        raise WorkerRequestError(message, retryable=retryable)
    raise AssertionError("worker request retry loop exhausted unexpectedly")


def stopped(job_id: str, state: dict[str, object]) -> bool:
    now = time.monotonic()
    if state.get("stopped"):
        return True
    if now - float(state.get("checked_at", 0.0)) < 2:
        return False
    lease_id = str(state.get("lease_id") or "")
    suffix = f"?lease_id={lease_id}" if lease_id else ""
    status = request_json(f"/api/workers/{WORKER_TARGET}/jobs/{job_id}/heartbeat{suffix}")
    state["checked_at"] = now
    state["stopped"] = bool(status.get("stop_requested"))
    return bool(state["stopped"])


def report_results(
    job_id: str, results: list[dict[str, Any]], lease_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, object] = {"results": results}
    if lease_id:
        payload["lease_id"] = lease_id
    return request_json(f"/api/workers/{WORKER_TARGET}/jobs/{job_id}/results", payload)


def report_result(job_id: str, result: dict[str, Any], lease_id: str | None = None) -> dict[str, Any]:
    return report_results(job_id, [result], lease_id)


def complete_job(
    job_id: str, lease_id: str | None = None, control_probes: list[dict[str, Any]] | None = None,
) -> None:
    """Complete a lease after every result callback has been durably acknowledged."""
    payload: dict[str, object] = {"results": []}
    if control_probes:
        payload["control_probes"] = control_probes
    if lease_id:
        payload["lease_id"] = lease_id
    request_json(f"/api/workers/{WORKER_TARGET}/jobs/{job_id}/complete", payload)


def skipped_result(email: str, index: int) -> dict[str, object]:
    return {
        "email": email,
        "original_index": index,
        "valid": False,
        "deliverable": None,
        "domain_type": "-",
        "verification_method": "stopped_after_deliverable",
        "smtp_result": "找到可投递候选地址后停止验证",
        "message": "找到可投递候选地址后停止验证",
        "skipped": True,
    }


def job_emails_and_indices(job: dict[str, object]) -> tuple[list[str], list[int]]:
    items = job.get("items")
    if isinstance(items, list):
        emails = [str(item["email"]) for item in items if isinstance(item, dict)]
        indices = [int(item["original_index"]) for item in items if isinstance(item, dict)]
        return emails, indices
    emails = [str(email) for email in job["emails"]]
    return emails, list(range(len(emails)))


def report_parent_timeout(job: dict[str, object], lease_id: str) -> None:
    """Persist retryable results and close a lease after the batch parent stalls."""
    emails, original_indices = job_emails_and_indices(job)
    retry_at = datetime.fromtimestamp(
        time.time() + settings.temporary_smtp_retry_seconds, timezone.utc
    ).isoformat()
    results: list[dict[str, Any]] = []
    for email, original_index in zip(emails, original_indices):
        result = timeout_result(email, original_index)
        result.update({
            "retry_state": "scheduled",
            "retry_attempt": 1,
            "retry_max_attempts": settings.temporary_smtp_immediate_retries,
            "retry_at": retry_at,
        })
        results.append(result)
    report_results(str(job["id"]), results, lease_id)
    complete_job(str(job["id"]), lease_id)


def _verify_job(job: dict[str, object], control: dict[str, object]) -> None:
    job_id = str(job["id"])
    emails, original_indices = job_emails_and_indices(job)
    lease_id = str(job.get("lease_id") or "") or None
    if WORKER_TARGET == "gmail":
        remote_limit = settings.cloudshell_worker_max_workers
    elif WORKER_TARGET == "codearts":
        remote_limit = settings.codearts_worker_max_workers
    else:
        remote_limit = settings.cloudstudio_worker_max_workers
    worker_count = max(1, min(int(job.get("worker_count", 1)), remote_limit))
    if any(is_qq_email(email) for email in emails):
        worker_count = max(1, min(settings.qq_worker_max_workers, len(emails)))
    completed_results: list[dict[str, Any]] = []
    pending_reports: list[dict[str, Any]] = []
    last_report_at = [time.monotonic()]

    def flush_reports() -> None:
        if not pending_reports:
            return
        response = report_results(job_id, list(pending_reports), lease_id)
        pending_reports.clear()
        last_report_at[0] = time.monotonic()
        control["stopped"] = bool(response.get("stop_requested"))

    def on_result(raw_result: dict[str, Any]) -> None:
        if stopped(job_id, control):
            return
        result = dict(raw_result)
        relative_index = int(result.get("original_index", 0))
        if 0 <= relative_index < len(original_indices):
            result["original_index"] = original_indices[relative_index]
            result["email"] = emails[relative_index]
        completed_results.append(dict(result))
        needs_retry = (
            is_retryable_smtp_result(result)
            and not is_smtp_greylisted(result)
        )
        if needs_retry:
            retry_at = time.time() + settings.temporary_smtp_retry_seconds
            result.update({
                "retry_state": "scheduled",
                "retry_attempt": 1,
                "retry_max_attempts": settings.temporary_smtp_immediate_retries,
                "retry_at": __import__("datetime").datetime.fromtimestamp(
                    retry_at, __import__("datetime").timezone.utc
                ).isoformat(),
            })
        # The first visible 4xx result must already include its retry schedule.
        pending_reports.append(result)
        if len(pending_reports) >= 8 or time.monotonic() - last_report_at[0] >= 5:
            flush_reports()

    if bool(job.get("stop_on_deliverable")):
        verifier = create_verifier(1)
        for index, email in enumerate(emails):
            if stopped(job_id, control):
                return
            batch = verifier.verify_batch_distributed(
                [email], num_processes=1, should_stop=lambda: stopped(job_id, control)
            )
            if stopped(job_id, control):
                return
            if not batch:
                continue
            result = dict(batch[0])
            result["original_index"] = original_indices[index]
            completed_results.append(dict(result))
            response = report_result(job_id, result, lease_id)
            control["stopped"] = bool(response.get("stop_requested"))
            if control["stopped"]:
                return
            if result.get("deliverable") is True:
                for remaining_index, remaining_email in enumerate(
                    emails[index + 1 :], index + 1
                ):
                    skipped = skipped_result(remaining_email, original_indices[remaining_index])
                    report_result(job_id, skipped, lease_id)
                break
    else:
        verifier = create_verifier(worker_count)
        verifier.verify_batch_distributed(
            emails,
            num_processes=worker_count,
            result_callback=on_result,
            should_stop=lambda: stopped(job_id, control),
        )
        flush_reports()
        if stopped(job_id, control):
            return
    if not stopped(job_id, control):
        control_probes: list[dict[str, Any]] = []
        control_email = str(job.get("control_probe_email") or "")
        if control_email and any(is_suspicious_recipient_rejection(item) for item in completed_results):
            verifier = create_verifier(1)
            probe = verifier.verify_batch_distributed([control_email], num_processes=1)
            if probe:
                control_probes.append({"email": control_email, "result": dict(probe[0])})
        complete_job(job_id, lease_id, control_probes)


def verify_job(job: dict[str, object]) -> None:
    """Keep a lease alive independently of SMTP result callbacks."""
    job_id = str(job["id"])
    lease_id = str(job.get("lease_id") or "")
    control: dict[str, object] = {"checked_at": 0.0, "stopped": False, "lease_id": lease_id}
    done = threading.Event()

    def heartbeat() -> None:
        while not done.wait(20):
            try:
                status = request_json(
                    f"/api/workers/{WORKER_TARGET}/jobs/{job_id}/heartbeat?lease_id={lease_id}"
                )
                control["checked_at"] = time.monotonic()
                control["stopped"] = bool(status.get("stop_requested"))
            except WorkerRequestError as exc:
                if not exc.retryable:
                    print(f"Remote lease heartbeat failed for {job_id}: {exc}", file=sys.stderr, flush=True)

    thread = threading.Thread(target=heartbeat, name=f"lease-heartbeat-{job_id}", daemon=True)
    thread.start()
    try:
        try:
            with email_verification_deadline(
                settings.email_hard_timeout_seconds + PARENT_TIMEOUT_GRACE_SECONDS
            ):
                _verify_job(job, control)
        except EmailVerificationTimeout:
            print(
                f"Remote worker parent timeout for {job_id}; scheduling retry",
                file=sys.stderr,
                flush=True,
            )
            report_parent_timeout(job, lease_id)
    finally:
        done.set()
        thread.join(timeout=2)


def main() -> None:
    if not TOKEN:
        raise SystemExit("VERIGO_TENCENT_QQ_WORKER_TOKEN is required")
    print(f"Verigo {WORKER_TARGET} worker {WORKER_ID} polling {SERVER_URL}", flush=True)
    while True:
        try:
            claim = request_json(
                f"/api/workers/{WORKER_TARGET}/claim?wait_seconds={CLAIM_WAIT_SECONDS}"
            )
            job = claim.get("job")
            if not job:
                time.sleep(POLL_SECONDS)
                continue
            try:
                verify_job(dict(job))
            except Exception as exc:
                job_id = str(dict(job)["id"])
                if isinstance(exc, WorkerRequestError) and exc.retryable:
                    # Do not turn a temporary worker-to-API outage into a failed
                    # verification. The lease will expire and the queue retries it.
                    print(
                        f"Remote worker connection interrupted for {job_id}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    time.sleep(RETRY_SECONDS)
                    continue
                try:
                    request_json(
                        f"/api/workers/{WORKER_TARGET}/jobs/{job_id}/fail",
                        {"error": f"{type(exc).__name__}: {exc}"[:500],
                         "lease_id": str(dict(job).get("lease_id") or "")},
                    )
                except WorkerRequestError:
                    pass
                print(f"Remote worker job {job_id} failed: {exc}", file=sys.stderr, flush=True)
        except WorkerRequestError as exc:
            print(f"Remote worker connection failed: {exc}", file=sys.stderr, flush=True)
            time.sleep(RETRY_SECONDS)


if __name__ == "__main__":
    main()
