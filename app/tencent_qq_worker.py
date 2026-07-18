from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from typing import Any

from app.core.legacy import create_verifier


SERVER_URL = os.getenv("VERIGO_TENCENT_QQ_SERVER", "https://verigo.site").rstrip("/")
TOKEN = os.getenv("VERIGO_TENCENT_QQ_WORKER_TOKEN", "")
WORKER_ID = os.getenv(
    "VERIGO_TENCENT_QQ_WORKER_ID", f"cloudstudio-{socket.gethostname()}-{os.getpid()}"
)
POLL_SECONDS = max(0.1, float(os.getenv("VERIGO_TENCENT_QQ_POLL_SECONDS", "0.25")))
RETRY_SECONDS = max(1.0, float(os.getenv("VERIGO_TENCENT_QQ_RETRY_SECONDS", "5")))


class WorkerRequestError(RuntimeError):
    pass


def request_json(path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    command = [
        "curl", "--silent", "--show-error", "--fail", "--max-time", "30",
        "-X", "POST", f"{SERVER_URL}{path}",
        "-H", "Content-Type: application/json",
        "-H", f"X-Verigo-Worker-Token: {TOKEN}",
        "-H", f"X-Verigo-Worker-Id: {WORKER_ID}",
    ]
    if payload is not None:
        command.extend(["--data-binary", json.dumps(payload, ensure_ascii=False)])
    try:
        response = subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=35
        )
        if response.returncode:
            raise WorkerRequestError(response.stderr.strip() or "curl request failed")
        return json.loads(response.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise WorkerRequestError(str(exc)) from exc


def stopped(job_id: str, state: dict[str, object]) -> bool:
    now = time.monotonic()
    if state.get("stopped"):
        return True
    if now - float(state.get("checked_at", 0.0)) < 2:
        return False
    status = request_json(f"/api/workers/tencent-qq/jobs/{job_id}/heartbeat")
    state["checked_at"] = now
    state["stopped"] = bool(status.get("stop_requested"))
    return bool(state["stopped"])


def report_result(job_id: str, result: dict[str, Any]) -> None:
    request_json(f"/api/workers/tencent-qq/jobs/{job_id}/results", {"results": [result]})


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


def verify_job(job: dict[str, object]) -> None:
    job_id = str(job["id"])
    emails = [str(email) for email in job["emails"]]
    worker_count = max(1, min(int(job.get("worker_count", 1)), 4))
    control: dict[str, object] = {"checked_at": 0.0, "stopped": False}
    results: list[dict[str, Any]] = []

    def on_result(raw_result: dict[str, Any]) -> None:
        if stopped(job_id, control):
            return
        result = dict(raw_result)
        results.append(result)
        report_result(job_id, result)

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
            result["original_index"] = index
            results.append(result)
            report_result(job_id, result)
            if result.get("deliverable") is True:
                for remaining_index, remaining_email in enumerate(emails[index + 1 :], index + 1):
                    skipped = skipped_result(remaining_email, remaining_index)
                    results.append(skipped)
                    report_result(job_id, skipped)
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
        request_json(f"/api/workers/tencent-qq/jobs/{job_id}/complete", {"results": results})


def main() -> None:
    if not TOKEN:
        raise SystemExit("VERIGO_TENCENT_QQ_WORKER_TOKEN is required")
    print(f"Verigo Tencent QQ worker {WORKER_ID} polling {SERVER_URL}", flush=True)
    while True:
        try:
            claim = request_json("/api/workers/tencent-qq/claim?wait_seconds=20")
            job = claim.get("job")
            if not job:
                time.sleep(POLL_SECONDS)
                continue
            try:
                verify_job(dict(job))
            except Exception as exc:
                job_id = str(dict(job)["id"])
                try:
                    request_json(
                        f"/api/workers/tencent-qq/jobs/{job_id}/fail",
                        {"error": f"{type(exc).__name__}: {exc}"[:500]},
                    )
                except WorkerRequestError:
                    pass
                print(f"Tencent QQ job {job_id} failed: {exc}", file=sys.stderr, flush=True)
        except WorkerRequestError as exc:
            print(f"Tencent QQ worker connection failed: {exc}", file=sys.stderr, flush=True)
            time.sleep(RETRY_SECONDS)


if __name__ == "__main__":
    main()
