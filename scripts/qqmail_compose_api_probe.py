"""Capture QQ Mail compose recipient-check APIs after a manual login."""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "tmp" / "qqmail_probe"
PROFILE_DIR = OUT_DIR / "chrome-profile"
EVENTS_PATH = OUT_DIR / "network.jsonl"
INTERESTING_PATH = OUT_DIR / "interesting.jsonl"
STATUS_PATH = OUT_DIR / "status.txt"

INTERESTING_RE = re.compile(
    r"addr|contact|compose|check|exist|uin|alias|foxmail|recv|recipient|"
    r"laddr|xmaddr|mailacct|suggest|complete|valid|invalid|nick|portrait|"
    r"qlogo|card|search|lookup",
    re.I,
)
SKIP_RESOURCE = {"image", "media", "font", "stylesheet"}
MAX_BODY = 80_000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_status(message: str) -> None:
    STATUS_PATH.write_text(f"{utc_now()} {message}\n", encoding="utf-8")
    print(message, flush=True)


def append_jsonl(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def interesting_url(url: str) -> bool:
    return bool(INTERESTING_RE.search(url))


def clip(text: str | None) -> str:
    if not text:
        return ""
    return text if len(text) <= MAX_BODY else text[:MAX_BODY] + "...[truncated]"


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    EVENTS_PATH.write_text("", encoding="utf-8")
    INTERESTING_PATH.write_text("", encoding="utf-8")

    write_status("launching headed Chromium; complete login in the opened window")
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()

        def on_response(response) -> None:
            request = response.request
            if request.resource_type in SKIP_RESOURCE:
                return
            url = response.url
            record = {
                "ts": utc_now(),
                "method": request.method,
                "status": response.status,
                "type": request.resource_type,
                "url": url,
                "post": clip(request.post_data),
            }
            try:
                content_type = (response.headers or {}).get("content-type", "")
            except Exception:
                content_type = ""
            record["content_type"] = content_type
            if request.resource_type in {"xhr", "fetch", "websocket"} or "json" in content_type or interesting_url(url):
                try:
                    record["body"] = clip(response.text())
                except Exception as exc:
                    record["body_error"] = type(exc).__name__
            append_jsonl(EVENTS_PATH, record)
            if interesting_url(url) or request.resource_type in {"xhr", "fetch"}:
                append_jsonl(INTERESTING_PATH, record)
                print(
                    f"[{record['status']}] {record['method']} {url}",
                    flush=True,
                )

        page.on("response", on_response)
        page.goto("https://wx.mail.qq.com/home/index#/compose", wait_until="domcontentloaded")
        write_status("browser opened at compose URL; waiting for login")

        logged_in = False
        deadline = time.time() + 15 * 60
        while time.time() < deadline:
            url = page.url
            title = ""
            try:
                title = page.title()
            except Exception:
                pass
            if (
                "wx.mail.qq.com/home/index" in url
                and "login" not in url.lower()
                and "login_jump" not in url.lower()
            ):
                # Compose or inbox shell after session is valid.
                try:
                    page.wait_for_selector("text=收件人", timeout=3000)
                    logged_in = True
                    write_status(f"login detected: {url}")
                    break
                except PlaywrightTimeoutError:
                    if "compose" in url or "#/" in url:
                        # Some sessions land on inbox first.
                        try:
                            if page.locator("text=写邮件").count() or page.locator("text=收件人").count():
                                logged_in = True
                                write_status(f"login detected via mailbox chrome: {url}")
                                break
                        except Exception:
                            pass
            write_status(f"waiting for login; current={url} title={title}")
            time.sleep(3)

        if not logged_in:
            write_status("login was not completed within 15 minutes")
            context.close()
            return 2

        if "compose" not in page.url:
            page.goto("https://wx.mail.qq.com/home/index#/compose", wait_until="domcontentloaded")
            write_status("navigated to compose after login")

        write_status(
            "ready. In the compose window, type the red/invalid recipients again "
            "(127873178231, 46712131111) plus one valid QQ number. Keep the window open."
        )
        # Keep capturing while the user reproduces the red-invalid UI.
        time.sleep(8 * 60)
        write_status("capture window finished")
        context.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
