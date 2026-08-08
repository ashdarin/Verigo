"""Exercise the extracted legacy CLI without network or child processes."""

from __future__ import annotations

from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.verification_cli import (
    configure_email_notification,
    run_verification_cli,
    send_results_email,
)


class FakeController:
    def __init__(self) -> None:
        self.sender_email = "sender@qq.com"
        self.sender_password = ""
        self.recipient_email = None
        self.user_max_processes = 8
        self.results: list[dict[str, object]] = []


controller = FakeController()
assert not configure_email_notification(controller, lambda _prompt: "not-an-email")
assert configure_email_notification(controller, lambda _prompt: "recipient@example.com")
assert controller.recipient_email == "recipient@example.com"
assert not send_results_email(controller, "missing.csv")

events: list[str] = []
run_verification_cli(
    FakeController,
    lambda: events.append("load"),
    lambda: events.append("save"),
    lambda _prompt: "7",
)
assert events == ["load", "save"]

print("verification cli smoke: ok")
