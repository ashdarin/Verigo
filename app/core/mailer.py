from __future__ import annotations

import smtplib
from email.message import EmailMessage

from app.config import settings


class MailNotConfiguredError(RuntimeError):
    pass


class MailDeliveryError(RuntimeError):
    pass


def send_password_reset_email(recipient: str, code: str) -> None:
    _send_code(recipient, code, "重置密码")


def send_email_verification(recipient: str, code: str) -> None:
    _send_code(recipient, code, "验证邮箱")


def _send_code(recipient: str, code: str, purpose: str) -> None:
    if not all((settings.mail_host, settings.mail_username, settings.mail_password, settings.mail_from)):
        raise MailNotConfiguredError("邮件服务尚未配置")
    message = EmailMessage()
    message["Subject"] = f"【Verigo】{purpose}验证码 {code}"
    message["From"] = settings.mail_from
    message["To"] = recipient
    message.set_content(
        f"验证码：{code}\n\n"
        f"{settings.password_reset_minutes} 分钟内有效。若不是你本人操作，请忽略此邮件。"
    )
    try:
        with smtplib.SMTP(settings.mail_host, settings.mail_port, timeout=15) as server:
            if settings.mail_starttls:
                server.starttls()
            server.login(settings.mail_username, settings.mail_password)
            server.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise MailDeliveryError("邮件暂时无法发送") from exc
