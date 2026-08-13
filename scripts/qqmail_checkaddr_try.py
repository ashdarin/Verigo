"""One-shot check of /send/checkaddr using the existing QQ Mail session."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "tmp" / "qqmail_probe"
PROFILE_DIR = OUT_DIR / "chrome-profile"
RESULT_PATH = OUT_DIR / "checkaddr_try.json"

TEST_EMAILS = [
    "127873178231@qq.com",
    "46712131111@qq.com",
    "1638174563@qq.com",
    "4671793@qq.com",
    "notexistaliasxyz123@foxmail.com",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    result: dict = {"ts": utc_now(), "ok": False}
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://wx.mail.qq.com/home/index#/compose", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        url = page.url
        result["url"] = url
        logged_in = "home/index" in url and "login" not in url.lower()
        result["logged_in"] = logged_in
        if not logged_in:
            RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False), flush=True)
            context.close()
            return 2

        cookies = {cookie["name"]: cookie["value"] for cookie in context.cookies()}
        result["cookie_names"] = sorted(cookies)
        result["has_sid_cookie"] = "xm_sid" in cookies
        payload = page.evaluate(
            """async (emails) => {
                const sid = (location.search.match(/[?&]sid=([^&]+)/) || [])[1]
                    || (document.cookie.match(/(?:^|; )xm_sid=([^;]+)/) || [])[1]
                    || "";
                const body = new URLSearchParams();
                for (const email of emails) body.append("email", email);
                body.set("language", "zh");
                body.set("r", String(Date.now()) + Math.floor(Math.random() * 1000));
                body.set("sid", sid);
                const response = await fetch("/send/checkaddr?sid=" + encodeURIComponent(sid), {
                    method: "POST",
                    credentials: "include",
                    headers: {
                        "Accept": "application/json, text/plain, */*",
                        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                    },
                    body: body.toString(),
                });
                const text = await response.text();
                let json = null;
                try { json = JSON.parse(text); } catch (error) {}
                return { status: response.status, sid_len: sid.length, text, json };
            }""",
            TEST_EMAILS,
        )
        result["ok"] = True
        result["emails"] = TEST_EMAILS
        result["response"] = payload
        RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        context.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
