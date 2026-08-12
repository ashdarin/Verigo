from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any

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
PARENT_TIMEOUT_GRACE_SECONDS = 15


class WorkerRequestError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


TRANSIENT_CURL_EXIT_CODES = frozenset({5, 6, 7, 18, 28, 52, 55, 56})
WORKER_REQUEST_ATTEMPTS = 4


def request_json(path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    command = [
        "curl", "--silent", "--show-error", "--fail", "--max-time", "30",
        "--connect-timeout", "10", "--retry", "3", "--retry-delay", "1",
        "--retry-connrefused",
        "-X", "POST", f"{SERVER_URL}{path}",
        "-H", "Content-Type: application/json",
        "-H", f"X-Verigo-Worker-Token: {TOKEN}",
        "-H", f"X-Verigo-Worker-Id: {WORKER_ID}",
        "-H", f"X-Verigo-Worker-Capacity: {WORKER_CAPACITY}",
    ]
    request_body = None
    if payload is not None:
        command.extend(["--data-binary", "@-"])
        request_body = json.dumps(payload, ensure_ascii=False)
    for attempt in range(WORKER_REQUEST_ATTEMPTS):
        try:
            response = subprocess.run(
                command,
                input=request_body,
                check=False,
                capture_output=True,
                text=True,
                timeout=35,
            )
            if not response.returncode:
                return json.loads(response.stdout)
            message = response.stderr.strip() or "curl request failed"
            retryable = response.returncode in TRANSIENT_CURL_EXIT_CODES or bool(
                response.returncode == 22
                and re.search(r"returned error: (408|429|500|502|503|504)\b", message)
            )
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            retryable = True
            message = str(exc)
        if retryable and attempt + 1 < WORKER_REQUEST_ATTEMPTS:
            time.sleep(2 ** attempt)
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
        worker_count = 1
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

    # Confirm ownership before expensive verifier initialization.  In
    # constrained remote workspaces imports or DNS setup can take longer than
    # the first SMTP operation, so waiting for the periodic thread would make
    # an otherwise live worker indistinguishable from a hung lease.
    initial_status = request_json(
        f"/api/workers/{WORKER_TARGET}/jobs/{job_id}/heartbeat?lease_id={lease_id}"
    )
    control["checked_at"] = time.monotonic()
    control["stopped"] = bool(initial_status.get("stop_requested"))
    if control["stopped"]:
        return

    def heartbeat() -> None:
        while not done.wait(10):
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
            claim = request_json(f"/api/workers/{WORKER_TARGET}/claim?wait_seconds=20")
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
