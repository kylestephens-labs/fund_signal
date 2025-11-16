import secrets

from pydantic import ConfigDict
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Application
    app_name: str = "FastAPI Template"
    app_version: str = "0.1.0"
    environment: str = "development"
    debug: bool = True
    log_level: str = "INFO"

    # Database
    database_url: str | None = None

    # Providers
    exa_api_key: str | None = None
    youcom_api_key: str | None = None
    tavily_api_key: str | None = None
    openai_api_key: str | None = None

    # Scoring/Runtime
    scoring_model: str = "gpt-4o-mini"
    scoring_system_prompt_path: str = "configs/scoring/system_prompt.md"
    scoring_temperature: float = 0.2
    fund_signal_mode: str = "fixture"

    # Storage
    supabase_url: str | None = None
    supabase_service_key: str | None = None
    supabase_bucket: str | None = None
    bundle_hmac_key: str | None = None
    proof_storage_bucket: str | None = None
    proof_cache_ttl_seconds: int = 300
    supabase_proof_qa_table: str | None = None
    proof_max_age_days: int = 90

    # UI/Test harness
    ui_base_url: str | None = None
    api_base_url: str | None = None

    # Security
    secret_key: str = ""  # Will be generated if empty
    cors_origins: list[str] = []  # Empty by default for security

    # Sentry
    sentry_dsn: str | None = None

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Proof QA job
    proof_qa_alert_webhook: str | None = None
    proof_qa_disable_alerts: bool = False
    proof_qa_concurrency: int = 25
    proof_qa_retry_limit: int = 3
    proof_qa_failure_threshold: float = 0.03

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Generate a random secret key if not provided
        if not self.secret_key:
            self.secret_key = secrets.token_urlsafe(32)

    model_config = ConfigDict(env_file=".env", case_sensitive=False)


settings = Settings()
