from __future__ import annotations

import os
from dataclasses import dataclass, fields as dc_fields
from pathlib import Path
from types import SimpleNamespace


BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("VERIGO_APP_NAME", "Verigo")
    # Zero disables the user-facing total-size limit. Remote workers are still
    # partitioned to keep each completion payload within its protocol limit.
    max_emails_per_job: int = int(os.getenv("VERIGO_MAX_EMAILS", "0"))
    remote_worker_max_emails_per_job: int = max(
        1, int(os.getenv("VERIGO_REMOTE_WORKER_MAX_EMAILS", "5000"))
    )
    remote_worker_max_workers: int = max(
        1, int(os.getenv("VERIGO_REMOTE_WORKER_MAX_WORKERS", "4"))
    )
    # Keep Cloud Studio at its established limit while allowing Cloud Shell to
    # use all eight processes in the Fastest mode.
    cloudstudio_worker_max_workers: int = max(
        1,
        int(
            os.getenv(
                "VERIGO_CLOUDSTUDIO_MAX_WORKERS",
                os.getenv("VERIGO_REMOTE_WORKER_MAX_WORKERS", "4"),
            )
        ),
    )
    # The supplied QQ verifier is designed to run six internal processes.
    # Keep this independent from the generic Cloud Studio task limit.
    qq_worker_max_workers: int = min(
        8, max(1, int(os.getenv("VERIGO_QQ_WORKER_MAX_WORKERS", "6")))
    )
    cloudshell_worker_max_workers: int = max(
        1, int(os.getenv("VERIGO_CLOUDSHELL_MAX_WORKERS", "25"))
    )
    codearts_worker_max_workers: int = max(
        1, int(os.getenv("VERIGO_CODEARTS_MAX_WORKERS", "16"))
    )
    # A remote worker process handles one lease at a time. Keep process count
    # separate from the per-shard verification parallelism above.
    cloudshell_worker_processes: int = min(
        8, max(1, int(os.getenv("VERIGO_CLOUDSHELL_WORKER_PROCESSES", "2")))
    )
    cloudshell_secondary_worker_processes: int = min(
        8,
        max(1, int(os.getenv("VERIGO_CLOUDSHELL_SECONDARY_WORKER_PROCESSES", "1"))),
    )
    scheduler_gmail_concurrency: int = max(1, int(os.getenv("VERIGO_SCHEDULER_GMAIL_CONCURRENCY", "25")))
    scheduler_microsoft_concurrency: int = max(1, int(os.getenv("VERIGO_SCHEDULER_MICROSOFT_CONCURRENCY", "64")))
    scheduler_default_domain_concurrency: int = max(1, int(os.getenv("VERIGO_SCHEDULER_DEFAULT_DOMAIN_CONCURRENCY", "4")))
    scheduler_domain_max_concurrency: int = max(
        1, int(os.getenv("VERIGO_SCHEDULER_DOMAIN_MAX_CONCURRENCY", "16"))
    )
    scheduler_remote_shard_size: int = max(
        1, int(os.getenv("VERIGO_SCHEDULER_REMOTE_SHARD_SIZE", "25"))
    )
    # A QQ lease must contain enough addresses to feed the verifier's internal
    # processes. Global receiver pressure is enforced separately by the MX
    # scheduler and its adaptive cooldown.
    qq_scheduler_shard_size: int = max(
        1,
        int(
            os.getenv(
                "VERIGO_QQ_SCHEDULER_SHARD_SIZE",
                os.getenv("VERIGO_QQ_WORKER_MAX_WORKERS", "6"),
            )
        ),
    )
    # Small discovery leases let several otherwise idle nodes share one
    # company domain while the MX scheduler remains the global safety limit.
    prospecting_scheduler_shard_size: int = max(
        1, int(os.getenv("VERIGO_PROSPECTING_SCHEDULER_SHARD_SIZE", "4"))
    )
    scheduler_claim_scan_limit: int = max(
        1, int(os.getenv("VERIGO_SCHEDULER_CLAIM_SCAN_LIMIT", "64"))
    )
    scheduler_successes_per_step: int = max(
        1, int(os.getenv("VERIGO_SCHEDULER_SUCCESSES_PER_STEP", "20"))
    )
    # Public mailbox providers recover independently after a pressure event.
    # Their lower threshold lets verified capacity return without changing the
    # conservative recovery behavior of ordinary business domains.
    scheduler_gmail_successes_per_step: int = max(
        1, int(os.getenv("VERIGO_SCHEDULER_GMAIL_SUCCESSES_PER_STEP", "8"))
    )
    scheduler_microsoft_successes_per_step: int = max(
        1, int(os.getenv("VERIGO_SCHEDULER_MICROSOFT_SUCCESSES_PER_STEP", "8"))
    )
    # Contact discovery is a controlled, opt-in workflow. Stable enterprise
    # MX hosts can ramp faster than ordinary verification, while the same
    # receiver-pressure safeguards remain authoritative.
    prospecting_scheduler_initial_domain_concurrency: int = max(
        1, int(os.getenv("VERIGO_PROSPECTING_SCHEDULER_INITIAL_DOMAIN_CONCURRENCY", "16"))
    )
    prospecting_scheduler_successes_per_step: int = max(
        1, int(os.getenv("VERIGO_PROSPECTING_SCHEDULER_SUCCESSES_PER_STEP", "8"))
    )
    prospecting_scheduler_step_size: int = max(
        1, int(os.getenv("VERIGO_PROSPECTING_SCHEDULER_STEP_SIZE", "2"))
    )
    scheduler_cooldown_seconds: int = max(
        1, int(os.getenv("VERIGO_SCHEDULER_COOLDOWN_SECONDS", "120"))
    )
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
    # Private beta for domain-only business contact discovery. It is disabled
    # unless explicitly enabled on the server and its allowlist is non-empty.
    prospecting_beta_enabled: bool = env_bool("VERIGO_PROSPECTING_BETA_ENABLED", False)
    prospecting_beta_allowed_emails: frozenset[str] = frozenset(
        email.strip().lower()
        for email in os.getenv("VERIGO_PROSPECTING_BETA_ALLOWED_EMAILS", "").split(",")
        if email.strip()
    )
    prospecting_beta_max_candidates: int = min(
        1000, max(100, int(os.getenv("VERIGO_PROSPECTING_BETA_MAX_CANDIDATES", "1000")))
    )
    # The per-run budget protects the verification queue. There is no daily
    # prospecting-run cap; fresh candidates are reserved per account and domain.
    prospecting_beta_catalogue_candidates: int = max(
        100, int(os.getenv("VERIGO_PROSPECTING_BETA_CATALOGUE_CANDIDATES", "10000"))
    )
    prospecting_company_import_max_rows: int = min(
        10_000, max(100, int(os.getenv("VERIGO_PROSPECTING_COMPANY_IMPORT_MAX_ROWS", "5000")))
    )
    # Domain prospecting deliberately backs off when a receiver signals that
    # recipient enumeration or rapid probing is unwelcome.
    prospecting_protection_cooldown_seconds: int = max(
        60, int(os.getenv("VERIGO_PROSPECTING_PROTECTION_COOLDOWN_SECONDS", "300"))
    )
    prospecting_protection_stop_seconds: int = max(
        300, int(os.getenv("VERIGO_PROSPECTING_PROTECTION_STOP_SECONDS", "86400"))
    )
    prospecting_protection_max_pressure_events: int = max(
        2, int(os.getenv("VERIGO_PROSPECTING_PROTECTION_MAX_PRESSURE_EVENTS", "4"))
    )
    prospecting_protection_generic_550_threshold: int = max(
        3, int(os.getenv("VERIGO_PROSPECTING_PROTECTION_GENERIC_550_THRESHOLD", "6"))
    )
    metrics_salt: str = os.getenv("VERIGO_METRICS_SALT", "")
    # Restricts operational queue and worker details to the local monitor.
    monitor_token: str = os.getenv("VERIGO_MONITOR_TOKEN", "")
    max_workers_per_job: int = int(os.getenv("VERIGO_MAX_WORKERS", "8"))
    max_parallel_jobs: int = int(os.getenv("VERIGO_MAX_PARALLEL_JOBS", "2"))
    verification_price_fen_per_100: int = max(
        1, int(os.getenv("VERIGO_VERIFICATION_PRICE_FEN_PER_100", "50"))
    )
    # A merchant-hosted checkout URL. It may contain {order_id}, {amount_fen},
    # {credits}, and {return_url}; payment confirmation remains the provider's
    # signed server-to-server responsibility.
    payment_checkout_url: str = os.getenv("VERIGO_PAYMENT_CHECKOUT_URL", "").strip()
    payment_webhook_token: str = os.getenv("VERIGO_PAYMENT_WEBHOOK_TOKEN", "")
    max_pending_jobs: int = int(os.getenv("VERIGO_MAX_PENDING_JOBS", "20"))
    qq_smtp_per_mx: int = max(
        1,
        int(
            os.getenv(
                "VERIGO_QQ_SMTP_PER_MX",
                os.getenv("VERIGO_QQ_WORKER_MAX_WORKERS", "6"),
            )
        ),
    )
    qq_smtp_wait_seconds: float = max(
        1.0, float(os.getenv("VERIGO_QQ_SMTP_WAIT_SECONDS", "300"))
    )
    email_hard_timeout_seconds: int = max(
        30, int(os.getenv("VERIGO_EMAIL_HARD_TIMEOUT_SECONDS", "90"))
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
    tencent_qq_worker_enabled: bool = env_bool("VERIGO_TENCENT_QQ_WORKER_ENABLED", False)
    tencent_qq_worker_allowed_emails: frozenset[str] = frozenset(
        email.strip().lower()
        for email in os.getenv("VERIGO_TENCENT_QQ_WORKER_ALLOWED_EMAILS", "").split(",")
        if email.strip()
    )
    gmail_worker_enabled: bool = env_bool("VERIGO_GMAIL_WORKER_ENABLED", False)
    gmail_worker_allowed_emails: frozenset[str] = frozenset(
        email.strip().lower()
        for email in os.getenv("VERIGO_GMAIL_WORKER_ALLOWED_EMAILS", "").split(",")
        if email.strip()
    )
    results_dir: Path = Path(
        os.getenv("VERIGO_RESULTS_DIR", str(BASE_DIR / "data" / "results"))
    )
    database_path: Path = Path(
        os.getenv("VERIGO_DATABASE_PATH", str(BASE_DIR / "data" / "verigo.db"))
    )
    # Application stores are PostgreSQL-only when a DSN is present.
    # SQLite is no longer a runtime backend for shared tables.
    postgres_enabled: bool = env_bool("VERIGO_POSTGRES_ENABLED", False)
    database_url: str = os.getenv("VERIGO_DATABASE_URL", "").strip()
    # Optional per-store overrides. Unset means "follow postgres_enabled".
    # Explicit false while the global switch is true is a configuration error.
    auth_postgres_enabled: bool | None = (
        env_bool("VERIGO_AUTH_POSTGRES_ENABLED")
        if os.getenv("VERIGO_AUTH_POSTGRES_ENABLED") is not None
        else None
    )
    metrics_postgres_enabled: bool | None = (
        env_bool("VERIGO_METRICS_POSTGRES_ENABLED")
        if os.getenv("VERIGO_METRICS_POSTGRES_ENABLED") is not None
        else None
    )
    prospecting_postgres_enabled: bool | None = (
        env_bool("VERIGO_PROSPECTING_POSTGRES_ENABLED")
        if os.getenv("VERIGO_PROSPECTING_POSTGRES_ENABLED") is not None
        else None
    )
    job_postgres_enabled: bool | None = (
        env_bool("VERIGO_JOB_POSTGRES_ENABLED")
        if os.getenv("VERIGO_JOB_POSTGRES_ENABLED") is not None
        else None
    )
    # A separately deployable, read-only dictionary. It deliberately remains
    # outside the transactional application database.
    name_catalog_path: Path = Path(
        os.getenv("VERIGO_NAME_CATALOG_PATH", str(BASE_DIR / "data" / "name_catalog.db"))
    )
    name_catalog_pool_size: int = max(
        100, int(os.getenv("VERIGO_NAME_CATALOG_POOL_SIZE", "10000"))
    )
    # The company catalogue is a separately managed, read-only analytical
    # database. It is never stored alongside account, billing, or job data.
    company_catalog_enabled: bool = env_bool("VERIGO_COMPANY_CATALOG_ENABLED", False)
    company_catalog_path: Path = Path(
        os.getenv("VERIGO_COMPANY_CATALOG_PATH", str(BASE_DIR / "data" / "company_catalog.duckdb"))
    )
    company_catalog_query_memory_limit: str = os.getenv(
        "VERIGO_COMPANY_CATALOG_QUERY_MEMORY_LIMIT", "512MB"
    )
    # Optional legacy Tinybird catalogue backend. Its token is server-only
    # and must never be exposed through the browser or static files.
    company_catalog_tinybird_url: str = os.getenv("VERIGO_COMPANY_CATALOG_TINYBIRD_URL", "").rstrip("/")
    company_catalog_tinybird_token: str = os.getenv("VERIGO_COMPANY_CATALOG_TINYBIRD_TOKEN", "")
    company_catalog_tinybird_timeout_seconds: float = max(
        1.0, float(os.getenv("VERIGO_COMPANY_CATALOG_TINYBIRD_TIMEOUT_SECONDS", "8"))
    )
    # Optional private Company Finder service. Verigo reaches it over a
    # server-to-server tunnel; it is never called by browser code.
    company_catalog_service_url: str = os.getenv("VERIGO_COMPANY_CATALOG_SERVICE_URL", "").rstrip("/")
    company_catalog_service_token: str = os.getenv("VERIGO_COMPANY_CATALOG_SERVICE_TOKEN", "")
    company_catalog_service_timeout_seconds: float = max(
        1.0, float(os.getenv("VERIGO_COMPANY_CATALOG_SERVICE_TIMEOUT_SECONDS", "8"))
    )
    smtp_limiter_path: Path = Path(
        os.getenv("VERIGO_SMTP_LIMITER_PATH", str(BASE_DIR / "data" / "smtp_limiter.db"))
    )
    smtp_helo_host: str = os.getenv("VERIGO_SMTP_HELO_HOST", "mail.verigo.site")
    smtp_mail_from: str = os.getenv("VERIGO_SMTP_MAIL_FROM", "verify@verigo.site")
    cloudstudio_probe_token: str = os.getenv("VERIGO_CLOUDSTUDIO_PROBE_TOKEN", "")
    tencent_qq_worker_token: str = os.getenv("VERIGO_TENCENT_QQ_WORKER_TOKEN", "")
    gmail_worker_token: str = os.getenv("VERIGO_GMAIL_WORKER_TOKEN", "")
    codearts_worker_enabled: bool = env_bool("VERIGO_CODEARTS_WORKER_ENABLED", False)
    codearts_worker_allowed_emails: frozenset[str] = frozenset(
        email.strip().lower()
        for email in os.getenv("VERIGO_CODEARTS_WORKER_ALLOWED_EMAILS", "").split(",")
        if email.strip()
    )
    codearts_worker_token: str = os.getenv("VERIGO_CODEARTS_WORKER_TOKEN", "")
    google_cloudshell_enabled: bool = env_bool("VERIGO_GOOGLE_CLOUDSHELL_ENABLED", False)
    google_cloudshell_user: str = os.getenv("VERIGO_GOOGLE_CLOUDSHELL_USER", "")
    google_cloudshell_quota_project: str = os.getenv("VERIGO_GOOGLE_CLOUDSHELL_QUOTA_PROJECT", "")
    google_cloudshell_adc_path: Path = Path(os.getenv("VERIGO_GOOGLE_CLOUDSHELL_ADC_PATH", ""))
    google_cloudshell_ssh_key_path: Path = Path(os.getenv("VERIGO_GOOGLE_CLOUDSHELL_SSH_KEY_PATH", ""))
    # Optional JSON manifest for additional Cloud Shell accounts. The manifest
    # contains paths to local ADC/SSH files, never passwords or one-time codes.
    google_cloudshell_accounts_file: Path = Path(
        os.getenv("VERIGO_GOOGLE_CLOUDSHELL_ACCOUNTS_FILE", "")
    )
    cloudshell_quota_cooldown_seconds: int = max(
        300, int(os.getenv("VERIGO_CLOUDSHELL_QUOTA_COOLDOWN_SECONDS", "3600"))
    )
    # Transient bootstrap failures should release their pool slot quickly.
    # Provider quota failures continue to use the longer cooldown above.
    cloudshell_bootstrap_cooldown_seconds: int = max(
        60, int(os.getenv("VERIGO_CLOUDSHELL_BOOTSTRAP_COOLDOWN_SECONDS", "300"))
    )
    cloudshell_coordinator_enabled: bool = env_bool(
        "VERIGO_CLOUDSHELL_COORDINATOR_ENABLED", True
    )
    cloudshell_active_min_accounts: int = max(
        1, int(os.getenv("VERIGO_CLOUDSHELL_ACTIVE_MIN_ACCOUNTS", "1"))
    )
    cloudshell_active_max_accounts: int = max(
        cloudshell_active_min_accounts,
        int(os.getenv("VERIGO_CLOUDSHELL_ACTIVE_MAX_ACCOUNTS", "4")),
    )
    cloudshell_queue_per_active_account: int = max(
        1, int(os.getenv("VERIGO_CLOUDSHELL_QUEUE_PER_ACTIVE_ACCOUNT", "2"))
    )
    # Pool expansion follows verification work, not just submitted-job count.
    # A single Gmail import may contain many thousands of pending addresses.
    cloudshell_pending_emails_per_active_account: int = max(
        1, int(os.getenv("VERIGO_CLOUDSHELL_PENDING_EMAILS_PER_ACTIVE_ACCOUNT", "48"))
    )
    # Cloud Shell has no StopEnvironment API. Stop Verigo's own remote
    # processes after this idle period so the platform can reclaim the shell.
    cloudshell_idle_stop_seconds: int = max(
        60, int(os.getenv("VERIGO_CLOUDSHELL_IDLE_STOP_SECONDS", "600"))
    )
    # Zero means no guessed quota. Set this only when an account's soft daily
    # budget is known; hard Cloud Shell quota remains provider-controlled.
    cloudshell_soft_quota_units: int = max(
        0, int(os.getenv("VERIGO_CLOUDSHELL_SOFT_QUOTA_UNITS", "0"))
    )
    google_cloudshell_secondary_enabled: bool = env_bool(
        "VERIGO_GOOGLE_CLOUDSHELL_SECONDARY_ENABLED", False
    )
    google_cloudshell_secondary_user: str = os.getenv(
        "VERIGO_GOOGLE_CLOUDSHELL_SECONDARY_USER", ""
    )
    google_cloudshell_secondary_quota_project: str = os.getenv(
        "VERIGO_GOOGLE_CLOUDSHELL_SECONDARY_QUOTA_PROJECT", ""
    )
    google_cloudshell_secondary_adc_path: Path = Path(
        os.getenv("VERIGO_GOOGLE_CLOUDSHELL_SECONDARY_ADC_PATH", "")
    )
    google_cloudshell_secondary_ssh_key_path: Path = Path(
        os.getenv("VERIGO_GOOGLE_CLOUDSHELL_SECONDARY_SSH_KEY_PATH", "")
    )
    google_cloudshell_secondary_ssh_known_hosts_path: Path = Path(
        os.getenv(
            "VERIGO_GOOGLE_CLOUDSHELL_SECONDARY_SSH_KNOWN_HOSTS_PATH",
            str(BASE_DIR / "data" / "cloudshell_account2_known_hosts"),
        )
    )
    cloudstudio_lifecycle_enabled: bool = env_bool(
        "VERIGO_CLOUDSTUDIO_LIFECYCLE_ENABLED", False
    )
    cloudstudio_secret_id: str = os.getenv("VERIGO_CLOUDSTUDIO_SECRET_ID", "")
    cloudstudio_secret_key: str = os.getenv("VERIGO_CLOUDSTUDIO_SECRET_KEY", "")
    cloudstudio_region: str = os.getenv("VERIGO_CLOUDSTUDIO_REGION", "")
    cloudstudio_space_key: str = os.getenv("VERIGO_CLOUDSTUDIO_SPACE_KEY", "")
    cloudstudio_ssh_enabled: bool = env_bool("VERIGO_CLOUDSTUDIO_SSH_ENABLED", False)
    cloudstudio_ssh_key_path: Path = Path(
        os.getenv("VERIGO_CLOUDSTUDIO_SSH_KEY_PATH", "")
    )
    cloudstudio_ssh_known_hosts_path: Path = Path(
        os.getenv("VERIGO_CLOUDSTUDIO_SSH_KNOWN_HOSTS_PATH", "")
    )
    cloudstudio_domestic_worker_enabled: bool = env_bool(
        "VERIGO_CLOUDSTUDIO_DOMESTIC_WORKER_ENABLED", False
    )
    cloudstudio_domestic_worker_token: str = os.getenv(
        "VERIGO_CLOUDSTUDIO_DOMESTIC_WORKER_TOKEN", ""
    )
    cloudstudio_secondary_lifecycle_enabled: bool = env_bool(
        "VERIGO_CLOUDSTUDIO_SECONDARY_LIFECYCLE_ENABLED", False
    )
    remote_worker_fallback_seconds: int = max(
        60, int(os.getenv("VERIGO_REMOTE_WORKER_FALLBACK_SECONDS", "180"))
    )
    cloudstudio_secondary_secret_id: str = os.getenv(
        "VERIGO_CLOUDSTUDIO_SECONDARY_SECRET_ID", ""
    )
    cloudstudio_secondary_secret_key: str = os.getenv(
        "VERIGO_CLOUDSTUDIO_SECONDARY_SECRET_KEY", ""
    )
    cloudstudio_secondary_region: str = os.getenv(
        "VERIGO_CLOUDSTUDIO_SECONDARY_REGION", ""
    )
    cloudstudio_secondary_space_key: str = os.getenv(
        "VERIGO_CLOUDSTUDIO_SECONDARY_SPACE_KEY", ""
    )
    cloudstudio_secondary_ssh_enabled: bool = env_bool(
        "VERIGO_CLOUDSTUDIO_SECONDARY_SSH_ENABLED", False
    )
    cloudstudio_secondary_ssh_key_path: Path = Path(
        os.getenv("VERIGO_CLOUDSTUDIO_SECONDARY_SSH_KEY_PATH", "")
    )
    cloudstudio_secondary_ssh_known_hosts_path: Path = Path(
        os.getenv("VERIGO_CLOUDSTUDIO_SECONDARY_SSH_KNOWN_HOSTS_PATH", "")
    )
    cloudstudio_ssh_token_expiry_seconds: int = max(
        60, int(os.getenv("VERIGO_CLOUDSTUDIO_SSH_TOKEN_EXPIRY_SECONDS", "300"))
    )
    cloudstudio_worker_online_seconds: int = max(
        15, int(os.getenv("VERIGO_CLOUDSTUDIO_WORKER_ONLINE_SECONDS", "45"))
    )
    cloudstudio_startup_timeout_seconds: int = max(
        30, int(os.getenv("VERIGO_CLOUDSTUDIO_STARTUP_TIMEOUT_SECONDS", "300"))
    )
    cloudstudio_wake_max_attempts: int = max(
        1, int(os.getenv("VERIGO_CLOUDSTUDIO_WAKE_MAX_ATTEMPTS", "3"))
    )
    cloudstudio_wake_retry_seconds: int = max(
        5, int(os.getenv("VERIGO_CLOUDSTUDIO_WAKE_RETRY_SECONDS", "15"))
    )
    cloudstudio_idle_stop_seconds: int = max(
        60, int(os.getenv("VERIGO_CLOUDSTUDIO_IDLE_STOP_SECONDS", "600"))
    )
    cloudstudio_lifecycle_poll_seconds: float = max(
        1.0, float(os.getenv("VERIGO_CLOUDSTUDIO_LIFECYCLE_POLL_SECONDS", "5"))
    )
    # Cloud Studio starts lifecycle work after the IDE terminal is attached.
    cloudstudio_session_activation_seconds: int = max(
        15, int(os.getenv("VERIGO_CLOUDSTUDIO_SESSION_ACTIVATION_SECONDS", "30"))
    )
    worker_poll_seconds: float = float(os.getenv("VERIGO_WORKER_POLL_SECONDS", "1"))
    worker_lease_seconds: int = int(os.getenv("VERIGO_WORKER_LEASE_SECONDS", "180"))
    sqlite_busy_timeout_ms: int = max(
        1_000, int(os.getenv("VERIGO_SQLITE_BUSY_TIMEOUT_MS", "30000"))
    )
    sqlite_write_retry_attempts: int = max(
        1, int(os.getenv("VERIGO_SQLITE_WRITE_RETRY_ATTEMPTS", "3"))
    )
    sqlite_write_retry_delay_ms: int = max(
        10, int(os.getenv("VERIGO_SQLITE_WRITE_RETRY_DELAY_MS", "100"))
    )
    sqlite_synchronous: str = os.getenv("VERIGO_SQLITE_SYNCHRONOUS", "NORMAL")
    sqlite_cache_size_kb: int = max(
        2000, int(os.getenv("VERIGO_SQLITE_CACHE_SIZE_KB", "10240"))
    )
    sqlite_mmap_size: int = int(os.getenv("VERIGO_SQLITE_MMAP_SIZE", "134217728"))
    sqlite_wal_autocheckpoint: int = max(
        100, int(os.getenv("VERIGO_SQLITE_WAL_AUTOCHECKPOINT", "5000"))
    )
    node_stale_seconds: int = max(
        30, int(os.getenv("VERIGO_NODE_STALE_SECONDS", "180"))
    )
    node_offline_seconds: int = max(
        60, int(os.getenv("VERIGO_NODE_OFFLINE_SECONDS", "540"))
    )
    temporary_smtp_immediate_retries: int = 3
    temporary_smtp_retry_seconds: float = 60.0
    smtp_greylist_retry_seconds: int = 300
    smtp_greylist_retry_max_attempts: int = 2
    # The public disposable-email API is advisory and queried only for an
    # unknown domain after local checks. It never changes SMTP deliverability.
    disposable_lookup_enabled: bool = env_bool("VERIGO_DISPOSABLE_LOOKUP_ENABLED", False)
    disposable_lookup_url: str = os.getenv(
        "VERIGO_DISPOSABLE_LOOKUP_URL", "https://disposable.debounce.io/"
    ).strip()
    disposable_lookup_timeout_seconds: float = min(
        2.0, max(0.1, float(os.getenv("VERIGO_DISPOSABLE_LOOKUP_TIMEOUT_SECONDS", "0.8")))
    )
    # The public lookup must never compete with SMTP verification. These are
    # per-process limits for best-effort background domain prefetches.
    disposable_lookup_background_workers: int = min(
        4, max(1, int(os.getenv("VERIGO_DISPOSABLE_LOOKUP_BACKGROUND_WORKERS", "2")))
    )
    disposable_lookup_background_queue: int = min(
        16, max(0, int(os.getenv("VERIGO_DISPOSABLE_LOOKUP_BACKGROUND_QUEUE", "2")))
    )
    disposable_lookup_positive_cache_hours: int = min(
        24 * 90, max(1, int(os.getenv("VERIGO_DISPOSABLE_LOOKUP_POSITIVE_CACHE_HOURS", "720")))
    )
    disposable_lookup_negative_cache_hours: int = min(
        24 * 30, max(1, int(os.getenv("VERIGO_DISPOSABLE_LOOKUP_NEGATIVE_CACHE_HOURS", "168")))
    )
    disposable_lookup_failure_cache_seconds: int = min(
        3600, max(1, int(os.getenv("VERIGO_DISPOSABLE_LOOKUP_FAILURE_CACHE_SECONDS", "300")))
    )
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

    def secondary_cloudstudio_namespace(self) -> SimpleNamespace:
        """Build a config namespace for the secondary CloudStudio workspace.

        Auto-maps cloudstudio_secondary_* fields to cloudstudio_* names so
        new secondary settings are picked up without manual SimpleNamespace
        updates in worker_lifecycle.py.
        """
        mapped: dict[str, object] = {}
        for f in dc_fields(self):
            if f.name.startswith("cloudstudio_secondary_"):
                target = f.name.replace("cloudstudio_secondary_", "cloudstudio_")
                mapped[target] = getattr(self, f.name)
        mapped["tencent_qq_worker_enabled"] = self.cloudstudio_domestic_worker_enabled
        mapped["tencent_qq_worker_token"] = self.cloudstudio_domestic_worker_token
        mapped["cloudstudio_probe_token"] = self.cloudstudio_probe_token
        mapped["cloudstudio_ssh_token_expiry_seconds"] = self.cloudstudio_ssh_token_expiry_seconds
        mapped["cloudstudio_worker_online_seconds"] = self.cloudstudio_worker_online_seconds
        mapped["cloudstudio_startup_timeout_seconds"] = self.cloudstudio_startup_timeout_seconds
        mapped["cloudstudio_wake_max_attempts"] = self.cloudstudio_wake_max_attempts
        mapped["cloudstudio_wake_retry_seconds"] = self.cloudstudio_wake_retry_seconds
        mapped["cloudstudio_idle_stop_seconds"] = self.cloudstudio_idle_stop_seconds
        mapped["cloudstudio_lifecycle_poll_seconds"] = self.cloudstudio_lifecycle_poll_seconds
        return SimpleNamespace(**mapped)


def _validate_postgres_settings(cfg: Settings) -> None:
    """Fail fast on cutover configuration conflicts."""
    store_flags = {
        "VERIGO_AUTH_POSTGRES_ENABLED": cfg.auth_postgres_enabled,
        "VERIGO_METRICS_POSTGRES_ENABLED": cfg.metrics_postgres_enabled,
        "VERIGO_PROSPECTING_POSTGRES_ENABLED": cfg.prospecting_postgres_enabled,
        "VERIGO_JOB_POSTGRES_ENABLED": cfg.job_postgres_enabled,
    }
    # A configured DSN means the process is PostgreSQL-only for app data.
    if cfg.database_url and not cfg.postgres_enabled:
        # Do not silently keep a SQLite fallback next to a live DSN.
        object.__setattr__(cfg, "postgres_enabled", True)
    if cfg.postgres_enabled:
        if not cfg.database_url:
            raise RuntimeError(
                "VERIGO_POSTGRES_ENABLED=true requires VERIGO_DATABASE_URL"
            )
        for name, value in store_flags.items():
            if value is False:
                raise RuntimeError(
                    f"{name}=false conflicts with VERIGO_POSTGRES_ENABLED=true; "
                    "unset the per-store flag or disable the global switch"
                )
    else:
        for name, value in store_flags.items():
            if value is True:
                raise RuntimeError(
                    f"{name}=true requires VERIGO_POSTGRES_ENABLED=true"
                )


settings = Settings()
_validate_postgres_settings(settings)
