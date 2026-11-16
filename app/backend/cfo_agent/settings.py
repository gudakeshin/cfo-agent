from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = BACKEND_ROOT / "config"
DATA_DIR = BACKEND_ROOT / "data"
OUTBOX_DIR = DATA_DIR / "outbox"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_ROOT.parent / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Model provider ───────────────────────────────────────────────────────
    # anthropic = Anthropic API (US/global endpoint)
    # bedrock   = Claude on AWS Bedrock in AWS_REGION (IAM credentials, no API key)
    # vertex    = Claude on Google Vertex AI in VERTEX_REGION (service-account credentials)
    llm_provider: str = Field(default="anthropic", alias="LLM_PROVIDER")
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    # Provider-specific model ID: e.g. claude-opus-4-8 (Anthropic), an inference
    # profile such as in.anthropic.claude-sonnet-5 (Bedrock), or claude-sonnet-5@<date> (Vertex)
    anthropic_model: str = Field(default="claude-opus-4-8", alias="ANTHROPIC_MODEL")
    aws_region: str | None = Field(default=None, alias="AWS_REGION")
    aws_profile: str | None = Field(default=None, alias="AWS_PROFILE")
    vertex_project_id: str | None = Field(default=None, alias="VERTEX_PROJECT_ID")
    vertex_region: str | None = Field(default=None, alias="VERTEX_REGION")
    # "india": refuse to start unless every model call is processed in India.
    data_residency: str = Field(default="", alias="DATA_RESIDENCY")
    # Regulatory feed web search: "same" (main provider, if it supports web search),
    # "anthropic" (Anthropic API — sends only public search topics), or "off".
    news_provider: str = Field(default="same", alias="NEWS_PROVIDER")
    # auto | native | tool — how structured JSON is requested (auto: native on the
    # Anthropic API, forced tool call on Bedrock/Vertex)
    structured_output_mode: str = Field(default="auto", alias="STRUCTURED_OUTPUT_MODE")

    database_url: str = Field(
        default=f"sqlite:///{DATA_DIR / 'cfo_agent.db'}",
        alias="DATABASE_URL",
    )

    resend_api_key: str | None = Field(default=None, alias="RESEND_API_KEY")
    email_from: str = Field(default="cfo-agent@localhost", alias="EMAIL_FROM")
    email_to: str = Field(default="cfo@localhost", alias="EMAIL_TO")
    smtp_host: str | None = Field(default=None, alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    smtp_user: str | None = Field(default=None, alias="SMTP_USER")
    smtp_password: str | None = Field(default=None, alias="SMTP_PASSWORD")

    digest_cron: str = Field(default="0 8 * * mon", alias="DIGEST_CRON")
    redline_cron: str = Field(default="*/15 * * * *", alias="REDLINE_CRON")
    news_cron: str = Field(default="30 7 * * *", alias="NEWS_CRON")
    news_model: str = Field(default="claude-sonnet-5", alias="NEWS_MODEL")
    timezone: str = Field(default="Asia/Kolkata", alias="TIMEZONE")
    # India's financial year runs April to March; set 1 for a calendar-year client.
    fiscal_year_start_month: int = Field(default=4, ge=1, le=12, alias="FISCAL_YEAR_START_MONTH")

    frontend_dist: Path = Field(
        default=BACKEND_ROOT.parent / "frontend" / "dist",
        alias="FRONTEND_DIST",
    )

    embedding_model: str = Field(
        default="sentence-transformers/all-MiniLM-L6-v2",
        alias="EMBEDDING_MODEL",
    )
    # 0 = auto: 1 connect + 2 per item (synth + one repair) + 1 headline
    max_llm_calls_per_run: int = Field(default=0, alias="MAX_LLM_CALLS_PER_RUN")
    max_llm_cost_usd_per_run: float = Field(default=5.0, alias="MAX_LLM_COST_USD_PER_RUN")
    llm_timeout_s: float = Field(default=180.0, alias="LLM_TIMEOUT_S")
    llm_max_retries: int = Field(default=3, alias="LLM_MAX_RETRIES")
    max_tool_rounds: int = 5
    # "replay" = demo: a replay clock walks the synthetic quarter week by week.
    # "live"   = real feed: alerts and digests track today's date / latest data.
    data_mode: str = Field(default="replay", alias="DATA_MODE")

    # ── Deployment & security ────────────────────────────────────────────────
    # DEMO_MODE enables demo-only endpoints (synthetic data regeneration, replay tools).
    demo_mode: bool = Field(default=True, alias="DEMO_MODE")
    # Comma-separated browser origins allowed to call the API cross-origin. Empty =
    # same-origin only (the API serves the UI itself).
    cors_origins: str = Field(default="", alias="CORS_ORIGINS")
    # Signs session cookies and derives the key that encrypts connector secrets.
    # Required (≥32 chars) whenever DEMO_MODE=false.
    secret_key: str | None = Field(default=None, alias="SECRET_KEY")

    # ── Sign-in ──────────────────────────────────────────────────────────────
    # off   = no sign-in; every request acts as an admin (local demo only)
    # local = email + password users from LOCAL_USERS_FILE
    # oidc  = single sign-on via any OpenID Connect provider (Entra ID, Okta, Google…)
    auth_mode: str = Field(default="off", alias="AUTH_MODE")
    local_users_file: Path = Field(default=CONFIG_DIR / "users.yaml", alias="LOCAL_USERS_FILE")
    oidc_issuer: str | None = Field(default=None, alias="OIDC_ISSUER")
    oidc_client_id: str | None = Field(default=None, alias="OIDC_CLIENT_ID")
    oidc_client_secret: str | None = Field(default=None, alias="OIDC_CLIENT_SECRET")
    oidc_scopes: str = Field(default="openid email profile", alias="OIDC_SCOPES")
    # Claim holding the user's groups/roles, and how its values map to app roles,
    # e.g. {"CFO-Office": "cfo", "Finance-Delegates": "delegate", "IT-Admins": "admin"}
    oidc_roles_claim: str = Field(default="roles", alias="OIDC_ROLES_CLAIM")
    oidc_role_map: str = Field(default="{}", alias="OIDC_ROLE_MAP")
    oidc_default_role: str = Field(default="viewer", alias="OIDC_DEFAULT_ROLE")
    public_base_url: str = Field(default="http://127.0.0.1:8000", alias="PUBLIC_BASE_URL")
    session_ttl_hours: int = Field(default=12, alias="SESSION_TTL_HOURS")

    # ── Worker split ─────────────────────────────────────────────────────────
    # RUN_SCHEDULER=false on API replicas; one dedicated worker process runs jobs.
    run_scheduler: bool = Field(default=True, alias="RUN_SCHEDULER")

    # ── Data protection ──────────────────────────────────────────────────────
    # Replace customer / vendor names with stable aliases before any LLM call and
    # restore them in the output, so names never leave the deployment.
    pii_masking: bool = Field(default=False, alias="PII_MASKING")

    # Root folder for the file-drop connector; each connector config names a sub-folder.
    csv_inbox_dir: Path = Field(default=DATA_DIR / "inbox", alias="CSV_INBOX_DIR")
    # Multi-tenant mode (one database per client); see cfo_agent/tenancy.py
    tenants_file: Path | None = Field(default=None, alias="TENANTS_FILE")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @model_validator(mode="after")
    def _key_required(self) -> "Settings":
        key = (self.anthropic_api_key or "").strip()
        needs_key = self.llm_provider == "anthropic" or self.news_provider == "anthropic"
        if needs_key and (not key or key.startswith("sk-ant-...")):
            raise ValueError(
                "ANTHROPIC_API_KEY is required. Copy .env.example to .env and set a real key."
            )
        self.anthropic_api_key = key or None
        return self

    @property
    def news_effective_provider(self) -> str | None:
        """Which provider runs the regulatory web search, or None if it is off.
        Web search is a server-side tool available on the Anthropic API."""
        if self.news_provider == "off":
            return None
        if self.news_provider == "anthropic":
            return "anthropic"
        return "anthropic" if self.llm_provider == "anthropic" else None


@lru_cache
def get_settings() -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    return Settings()  # type: ignore[call-arg]
