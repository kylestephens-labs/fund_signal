from __future__ import annotations

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
    supabase_db_host: str | None = None
    supabase_db_port: int | None = None
    supabase_db_user: str | None = None
    supabase_db_password: str | None = None
    pooled_supabase_dsn: str | None = None
    db_pool_min_size: int = 1
    db_pool_max_size: int = 5

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
    supabase_proof_replay_table: str | None = None
    proof_max_age_days: int = 90

    # UI/Test harness
    ui_base_url: str | None = None
    api_base_url: str | None = None
    next_public_api_base_url: str | None = None
    pgsslmode: str | None = None

    # Delivery / Day-3 pipelines
    delivery_scoring_run: str | None = None
    delivery_force_refresh: bool = False
    delivery_output_dir: str = "output"
    email_from: str | None = None
    email_to: str | None = None
    email_cc: str | None = None
    email_bcc: str | None = None
    email_subject: str | None = None
    email_feedback_to: str | None = None
    email_smtp_url: str | None = None
    email_disable_tls: bool = False
    delivery_email_force_run: bool = False
    slack_webhook_url: str | None = None
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_plan_solo: str | None = None
    stripe_plan_growth: str | None = None
    stripe_plan_team: str | None = None
    stripe_api_key: str | None = None  # legacy env key for compatibility
    stripe_publishable_key: str | None = None  # legacy env key for compatibility
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_redirect_uri: str | None = None

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
    proof_replay_alert_webhook: str | None = None
    proof_replay_disable_alerts: bool = False
    proof_replay_concurrency: int = 10
    proof_replay_max_redirects: int = 5
    proof_replay_failure_threshold: float = 0.01
    proof_replay_schedule_cron: str | None = None
    metrics_backend: str = "stdout"
    metrics_namespace: str = "proof_links"
    metrics_disable: bool = False
    metrics_sample_rate: float = 1.0
    metrics_statsd_host: str = "127.0.0.1"
    metrics_statsd_port: int = 8125
    metrics_schema_version: str = "proof-links.v1"
    render_alert_threshold_p95: float = 300.0
    render_alert_threshold_error: float = 0.05

    # Auth controls
    auth_rate_limit_window_seconds: int = 60
    auth_rate_limit_max_requests: int = 5
    cancel_undo_ttl_seconds: int = 172800

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Generate a random secret key if not provided
        if not self.secret_key:
            self.secret_key = secrets.token_urlsafe(32)

    @property
    def auth_allowed_plans(self) -> set[str]:
        """Return configured plan ids or the default symbolic plans when unset."""
        configured = {
            plan
            for plan in (
                self.stripe_plan_solo,
                self.stripe_plan_growth,
                self.stripe_plan_team,
            )
            if plan
        }
        if configured:
            return configured
        return {"solo", "growth", "team"}

    model_config = ConfigDict(env_file=".env", case_sensitive=False)


settings = Settings()
