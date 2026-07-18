from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("VERIGO_APP_NAME", "Verigo")
    max_emails_per_job: int = int(os.getenv("VERIGO_MAX_EMAILS", "5000"))
    max_guest_emails: int = int(os.getenv("VERIGO_MAX_GUEST_EMAILS", "100"))
    free_single_daily_limit: int = int(
        os.getenv("VERIGO_FREE_SINGLE_DAILY_LIMIT", "20")
    )
    anonymous_free_single_daily_limit: int = int(
        os.getenv("VERIGO_ANONYMOUS_FREE_SINGLE_DAILY_LIMIT", "10")
    )
    email_verification_trial_credits: int = int(
        os.getenv("VERIGO_EMAIL_VERIFICATION_TRIAL_CREDITS", "10")
    )
    trial_credit_days: int = int(os.getenv("VERIGO_TRIAL_CREDIT_DAYS", "7"))
    trial_network_limit: int = int(os.getenv("VERIGO_TRIAL_NETWORK_LIMIT", "2"))
    trial_network_window_days: int = int(
        os.getenv("VERIGO_TRIAL_NETWORK_WINDOW_DAYS", "7")
    )
    blocked_email_domains: frozenset[str] = frozenset(
        domain.strip().lower()
        for domain in os.getenv(
            "VERIGO_BLOCKED_EMAIL_DOMAINS",
            "mailinator.com,yopmail.com,guerrillamail.com,guerrillamail.info,"
            "tempmail.com,temp-mail.org,10minutemail.com,getnada.com,dispostable.com",
        ).split(",")
        if domain.strip()
    )
    turnstile_site_key: str = os.getenv("VERIGO_TURNSTILE_SITE_KEY", "")
    turnstile_secret_key: str = os.getenv("VERIGO_TURNSTILE_SECRET_KEY", "")
    admin_emails: frozenset[str] = frozenset(
        email.strip().lower()
        for email in os.getenv("VERIGO_ADMIN_EMAILS", "").split(",")
        if email.strip()
    )
    metrics_salt: str = os.getenv("VERIGO_METRICS_SALT", "")
    max_workers_per_job: int = int(os.getenv("VERIGO_MAX_WORKERS", "8"))
    max_parallel_jobs: int = int(os.getenv("VERIGO_MAX_PARALLEL_JOBS", "2"))
    max_pending_jobs: int = int(os.getenv("VERIGO_MAX_PENDING_JOBS", "20"))
    qq_smtp_per_mx: int = max(1, int(os.getenv("VERIGO_QQ_SMTP_PER_MX", "1")))
    qq_smtp_wait_seconds: float = max(
        1.0, float(os.getenv("VERIGO_QQ_SMTP_WAIT_SECONDS", "300"))
    )
    qq_backoff_base_seconds: float = max(
        1.0, float(os.getenv("VERIGO_QQ_BACKOFF_BASE_SECONDS", "30"))
    )
    qq_backoff_max_seconds: float = max(
        1.0, float(os.getenv("VERIGO_QQ_BACKOFF_MAX_SECONDS", "900"))
    )
    qq_avatar_timeout_seconds: float = max(
        1.0, float(os.getenv("VERIGO_QQ_AVATAR_TIMEOUT_SECONDS", "8"))
    )
    qq_avatar_wait_seconds: float = max(
        1.0, float(os.getenv("VERIGO_QQ_AVATAR_WAIT_SECONDS", "20"))
    )
    qq_avatar_min_interval_seconds: float = max(
        0.0, float(os.getenv("VERIGO_QQ_AVATAR_MIN_INTERVAL_SECONDS", "1"))
    )
    results_dir: Path = Path(
        os.getenv("VERIGO_RESULTS_DIR", str(BASE_DIR / "data" / "results"))
    )
    database_path: Path = Path(
        os.getenv("VERIGO_DATABASE_PATH", str(BASE_DIR / "data" / "verigo.db"))
    )
    smtp_limiter_path: Path = Path(
        os.getenv("VERIGO_SMTP_LIMITER_PATH", str(BASE_DIR / "data" / "smtp_limiter.db"))
    )
    smtp_helo_host: str = os.getenv("VERIGO_SMTP_HELO_HOST", "mail.verigo.site")
    smtp_mail_from: str = os.getenv("VERIGO_SMTP_MAIL_FROM", "verify@verigo.site")
    cloudstudio_probe_token: str = os.getenv("VERIGO_CLOUDSTUDIO_PROBE_TOKEN", "")
    tencent_qq_worker_token: str = os.getenv("VERIGO_TENCENT_QQ_WORKER_TOKEN", "")
    worker_poll_seconds: float = float(os.getenv("VERIGO_WORKER_POLL_SECONDS", "1"))
    worker_lease_seconds: int = int(os.getenv("VERIGO_WORKER_LEASE_SECONDS", "180"))
    verification_cache_hours: int = int(os.getenv("VERIGO_VERIFICATION_CACHE_HOURS", "24"))
    verified_email_recheck_days: int = int(os.getenv("VERIGO_VERIFIED_EMAIL_RECHECK_DAYS", "30"))
    mail_host: str = os.getenv("VERIGO_MAIL_HOST", "")
    mail_port: int = int(os.getenv("VERIGO_MAIL_PORT", "587"))
    mail_username: str = os.getenv("VERIGO_MAIL_USERNAME", "")
    mail_password: str = os.getenv("VERIGO_MAIL_PASSWORD", "")
    mail_from: str = os.getenv("VERIGO_MAIL_FROM", "")
    mail_starttls: bool = env_bool("VERIGO_MAIL_STARTTLS", True)
    password_reset_minutes: int = int(os.getenv("VERIGO_PASSWORD_RESET_MINUTES", "15"))
    max_import_bytes: int = int(os.getenv("VERIGO_MAX_IMPORT_BYTES", str(5 * 1024 * 1024)))
    session_cookie_name: str = os.getenv("VERIGO_SESSION_COOKIE", "verigo_session")
    session_ttl_days: int = int(os.getenv("VERIGO_SESSION_TTL_DAYS", "30"))
    secure_cookies: bool = env_bool("VERIGO_SECURE_COOKIES", False)


settings = Settings()
