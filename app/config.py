"""Settings, read from environment variables (or a .env file in the project root)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", f"sqlite:///{ROOT / 'sinag.db'}"))
    companies_file: Path = field(default_factory=lambda: Path(os.getenv("COMPANIES_FILE", str(ROOT / "companies.yaml"))))

    # Crawling
    crawl_interval_minutes: int = field(default_factory=lambda: int(os.getenv("CRAWL_INTERVAL_MINUTES", "15")))
    crawl_concurrency: int = field(default_factory=lambda: int(os.getenv("CRAWL_CONCURRENCY", "8")))
    per_host_delay_seconds: float = field(default_factory=lambda: float(os.getenv("PER_HOST_DELAY_SECONDS", "1.0")))
    http_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("HTTP_TIMEOUT_SECONDS", "20")))
    user_agent: str = field(default_factory=lambda: os.getenv(
        "USER_AGENT", "SinagJobsBot/1.0 (+https://sinagjobs.ph/bot; jobs@sinagjobs.ph)"))
    # Close a job once it has been missing from this many successful crawls of its company.
    close_after_missed_crawls: int = field(default_factory=lambda: int(os.getenv("CLOSE_AFTER_MISSED_CRAWLS", "2")))

    # Which jobs count as "Philippine" jobs
    include_apac_remote: bool = field(default_factory=lambda: _bool("INCLUDE_APAC_REMOTE", True))
    include_worldwide_remote: bool = field(default_factory=lambda: _bool("INCLUDE_WORLDWIDE_REMOTE", False))

    # Salary conversion to PHP per month
    usd_to_php: float = field(default_factory=lambda: float(os.getenv("USD_TO_PHP", "58.0")))
    aud_to_php: float = field(default_factory=lambda: float(os.getenv("AUD_TO_PHP", "38.0")))
    sgd_to_php: float = field(default_factory=lambda: float(os.getenv("SGD_TO_PHP", "44.0")))
    eur_to_php: float = field(default_factory=lambda: float(os.getenv("EUR_TO_PHP", "63.0")))
    gbp_to_php: float = field(default_factory=lambda: float(os.getenv("GBP_TO_PHP", "75.0")))

    # API
    cors_origins: list[str] = field(default_factory=lambda: [
        o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()])
    admin_token: str = field(default_factory=lambda: os.getenv("ADMIN_TOKEN", ""))

    # Site address used in emails, e.g. https://sinagjobs.ph
    public_url: str = field(default_factory=lambda: os.getenv(
        "PUBLIC_URL", f"https://{os.getenv('DOMAIN')}" if os.getenv("DOMAIN", "localhost") != "localhost"
        else "http://localhost:8000").rstrip("/"))
    # Send the login cookie only over HTTPS. On by default whenever DOMAIN is a real domain.
    cookie_secure: bool = field(default_factory=lambda: _bool(
        "COOKIE_SECURE", os.getenv("DOMAIN", "localhost") != "localhost"))
    session_days: int = field(default_factory=lambda: int(os.getenv("SESSION_DAYS", "30")))

    # Uploaded resumes, certificates and portfolios
    upload_dir: Path = field(default_factory=lambda: Path(os.getenv("UPLOAD_DIR", str(ROOT / "uploads"))))
    max_upload_mb: int = field(default_factory=lambda: int(os.getenv("MAX_UPLOAD_MB", "5")))
    max_documents_per_user: int = field(default_factory=lambda: int(os.getenv("MAX_DOCUMENTS_PER_USER", "20")))

    # Email (any SMTP service: Amazon SES, Postmark, Brevo, Gmail Workspace...). Empty host = emails are only logged.
    smtp_host: str = field(default_factory=lambda: os.getenv("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: int(os.getenv("SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: os.getenv("SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: os.getenv("SMTP_PASSWORD", ""))
    smtp_from: str = field(default_factory=lambda: os.getenv("SMTP_FROM", "Sinag Jobs <no-reply@localhost>"))
    smtp_starttls: bool = field(default_factory=lambda: _bool("SMTP_STARTTLS", True))

    # People must confirm their email before applying, posting jobs or getting alerts
    require_email_verification: bool = field(default_factory=lambda: _bool("REQUIRE_EMAIL_VERIFICATION", True))

    # Browser push notifications. Generate with: python -m app.cli vapid-keys
    vapid_public_key: str = field(default_factory=lambda: os.getenv("VAPID_PUBLIC_KEY", ""))
    vapid_private_key: str = field(default_factory=lambda: os.getenv("VAPID_PRIVATE_KEY", ""))
    vapid_subject: str = field(default_factory=lambda: os.getenv("VAPID_SUBJECT", "mailto:admin@localhost"))

    # Optional AI (Claude API). Without a key, screening and matching use plain rules.
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    anthropic_model: str = field(default_factory=lambda: os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5"))

    # Billing. Payments on this site are ALWAYS test payments: either the built-in simulator ("sandbox")
    # or PayMongo in test mode ("paymongo_test", which refuses live keys). No real money can move.
    billing_enabled: bool = field(default_factory=lambda: _bool("BILLING_ENABLED", True))
    payment_mode: str = field(default_factory=lambda: os.getenv("PAYMENT_MODE", "sandbox"))
    paymongo_secret_key: str = field(default_factory=lambda: os.getenv("PAYMONGO_SECRET_KEY", ""))
    paymongo_webhook_secret: str = field(default_factory=lambda: os.getenv("PAYMONGO_WEBHOOK_SECRET", ""))
    paymongo_methods: list[str] = field(default_factory=lambda: [
        m.strip() for m in os.getenv("PAYMONGO_METHODS", "card,gcash,paymaya,qrph").split(",") if m.strip()])
    free_live_jobs: int = field(default_factory=lambda: int(os.getenv("FREE_LIVE_JOBS", "1")))

    # Sign in with Google / Facebook / LinkedIn. A button appears only when its keys are set.
    google_client_id: str = field(default_factory=lambda: os.getenv("GOOGLE_CLIENT_ID", ""))
    google_client_secret: str = field(default_factory=lambda: os.getenv("GOOGLE_CLIENT_SECRET", ""))
    facebook_app_id: str = field(default_factory=lambda: os.getenv("FACEBOOK_APP_ID", ""))
    facebook_app_secret: str = field(default_factory=lambda: os.getenv("FACEBOOK_APP_SECRET", ""))
    facebook_graph_version: str = field(default_factory=lambda: os.getenv("FACEBOOK_GRAPH_VERSION", "v21.0"))
    linkedin_client_id: str = field(default_factory=lambda: os.getenv("LINKEDIN_CLIENT_ID", ""))
    linkedin_client_secret: str = field(default_factory=lambda: os.getenv("LINKEDIN_CLIENT_SECRET", ""))

    # Seller details printed on invoices (all invoices are TEST invoices, not BIR official receipts)
    seller_name: str = field(default_factory=lambda: os.getenv("SELLER_NAME", "Sinag Jobs"))
    seller_address: str = field(default_factory=lambda: os.getenv("SELLER_ADDRESS", ""))
    seller_tin: str = field(default_factory=lambda: os.getenv("SELLER_TIN", ""))
    vat_registered: bool = field(default_factory=lambda: _bool("VAT_REGISTERED", False))

    # Phone app: its link scheme (social sign-in returns to the app through it)
    app_scheme: str = field(default_factory=lambda: os.getenv("APP_SCHEME", "ph.sinagjobs.app"))
    # Phone app push (Firebase Cloud Messaging, for the Android and iPhone app)
    fcm_project_id: str = field(default_factory=lambda: os.getenv("FCM_PROJECT_ID", ""))
    fcm_service_account_file: str = field(default_factory=lambda: os.getenv("FCM_SERVICE_ACCOUNT_FILE", ""))

    # Job alerts
    timezone: str = field(default_factory=lambda: os.getenv("TIMEZONE", "Asia/Manila"))
    digest_hour: int = field(default_factory=lambda: int(os.getenv("DIGEST_HOUR", "8")))
    match_threshold: int = field(default_factory=lambda: int(os.getenv("MATCH_THRESHOLD", "60")))

    def fx(self, currency: str) -> float | None:
        return {
            "PHP": 1.0, "USD": self.usd_to_php, "AUD": self.aud_to_php, "SGD": self.sgd_to_php,
            "EUR": self.eur_to_php, "GBP": self.gbp_to_php,
        }.get((currency or "").upper())


settings = Settings()
