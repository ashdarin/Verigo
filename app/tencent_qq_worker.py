from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from typing import Any

from app.config import settings
from app.core.legacy import create_verifier
from app.core.provider_policy import is_qq_email
from app.core.result_retry import (
    is_retryable_smtp_result,
    is_smtp_greylisted,
    smtp_temporary_status,
)


WORKER_TARGET = os.getenv("VERIGO_REMOTE_WORKER_TARGET", "tencent-qq")
SERVER_URL = os.getenv("VERIGO_REMOTE_WORKER_SERVER", os.getenv("VERIGO_TENCENT_QQ_SERVER", "https://verigo.site")).rstrip("/")
TOKEN = os.getenv("VERIGO_REMOTE_WORKER_TOKEN", os.getenv("VERIGO_TENCENT_QQ_WORKER_TOKEN", ""))
WORKER_ID = os.getenv(
    "VERIGO_TENCENT_QQ_WORKER_ID", f"cloudstudio-{socket.gethostname()}-{os.getpid()}"
)
POLL_SECONDS = max(0.1, float(os.getenv("VERIGO_TENCENT_QQ_POLL_SECONDS", "0.25")))
RETRY_SECONDS = max(1.0, float(os.getenv("VERIGO_TENCENT_QQ_RETRY_SECONDS", "5")))


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


def report_result(job_id: str, result: dict[str, Any], lease_id: str | None = None) -> None:
    payload: dict[str, object] = {"results": [result]}
    if lease_id:
        payload["lease_id"] = lease_id
    request_json(f"/api/workers/{WORKER_TARGET}/jobs/{job_id}/results", payload)


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


def retry_temporary_smtp_results(
    job_id: str,
    emails: list[str],
    original_indices: list[int],
    results: list[dict[str, Any]],
    control: dict[str, object],
) -> list[dict[str, Any]]:
    by_index = {int(result.get("original_index", index)): dict(result) for index, result in enumerate(results)}
    for attempt in range(settings.temporary_smtp_immediate_retries):
        retry_items = [
            (index, emails[original_indices.index(index)])
            for index, result in by_index.items()
            if (
                0 <= index < len(emails)
                and is_retryable_smtp_result(result)
                and not is_smtp_greylisted(result)
            )
        ]
        if not retry_items or stopped(job_id, control):
            return [by_index[index] for index in sorted(by_index)]

        delay = settings.temporary_smtp_retry_seconds
        print(
            f"Retrying {len(retry_items)} temporary SMTP results for {job_id} after {delay:.1f}s",
            flush=True,
        )
        time.sleep(delay)
        verifier = create_verifier(1)
        retry_results = verifier.verify_batch_distributed(
            [email for _, email in retry_items],
            num_processes=1,
            should_stop=lambda: stopped(job_id, control),
        )
        if stopped(job_id, control):
            return [by_index[index] for index in sorted(by_index)]
        for retry_result in retry_results:
            result = dict(retry_result)
            relative_index = int(result.get("original_index", 0))
            if 0 <= relative_index < len(retry_items):
                original_index = retry_items[relative_index][0]
                result["original_index"] = original_index
                by_index[original_index] = result
                report_result(job_id, result, str(control.get("lease_id") or "") or None)
    for result in by_index.values():
        if is_retryable_smtp_result(result) and not is_smtp_greylisted(result):
            result["temporary_smtp_retry_count"] = settings.temporary_smtp_immediate_retries
    return [by_index[index] for index in sorted(by_index)]


def verify_job(job: dict[str, object]) -> None:
    job_id = str(job["id"])
    items = job.get("items")
    if isinstance(items, list):
        emails = [str(item["email"]) for item in items if isinstance(item, dict)]
        original_indices = [int(item["original_index"]) for item in items if isinstance(item, dict)]
    else:
        emails = [str(email) for email in job["emails"]]
        original_indices = list(range(len(emails)))
    lease_id = str(job.get("lease_id") or "") or None
    remote_limit = (
        settings.cloudshell_worker_max_workers
        if WORKER_TARGET == "gmail"
        else settings.cloudstudio_worker_max_workers
    )
    worker_count = max(1, min(int(job.get("worker_count", 1)), remote_limit))
    if any(is_qq_email(email) for email in emails):
        worker_count = 1
    control: dict[str, object] = {"checked_at": 0.0, "stopped": False, "lease_id": lease_id}
    results: list[dict[str, Any]] = []
    def on_result(raw_result: dict[str, Any]) -> None:
        if stopped(job_id, control):
            return
        result = dict(raw_result)
        relative_index = int(result.get("original_index", 0))
        if 0 <= relative_index < len(original_indices):
            result["original_index"] = original_indices[relative_index]
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
        results.append(result)
        # The first visible 4xx result must already include its retry schedule.
        report_result(job_id, result, lease_id)

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
            results.append(result)
            report_result(job_id, result, lease_id)
            if result.get("deliverable") is True:
                for remaining_index, remaining_email in enumerate(
                    emails[index + 1 :], index + 1
                ):
                    skipped = skipped_result(remaining_email, original_indices[remaining_index])
                    results.append(skipped)
                    report_result(job_id, skipped, lease_id)
                break
    else:
        verifier = create_verifier(worker_count)
        results = verifier.verify_batch_distributed(
            emails,
            num_processes=worker_count,
            result_callback=on_result,
            should_stop=lambda: stopped(job_id, control),
        )
        if stopped(job_id, control):
            return

    if not stopped(job_id, control):
        payload: dict[str, object] = {"results": results}
        if lease_id:
            payload["lease_id"] = lease_id
        request_json(f"/api/workers/{WORKER_TARGET}/jobs/{job_id}/complete", payload)


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
                        {"error": f"{type(exc).__name__}: {exc}"[:500]},
                    )
                except WorkerRequestError:
                    pass
                print(f"Remote worker job {job_id} failed: {exc}", file=sys.stderr, flush=True)
        except WorkerRequestError as exc:
            print(f"Remote worker connection failed: {exc}", file=sys.stderr, flush=True)
            time.sleep(RETRY_SECONDS)


if __name__ == "__main__":
    main()
