from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
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

    anthropic_api_key: str = Field(..., alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-opus-4-8", alias="ANTHROPIC_MODEL")

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

    frontend_dist: Path = Field(
        default=BACKEND_ROOT.parent / "frontend" / "dist",
        alias="FRONTEND_DIST",
    )

    embedding_model: str = Field(
        default="sentence-transformers/all-MiniLM-L6-v2",
        alias="EMBEDDING_MODEL",
    )
    max_llm_calls_per_run: int = 8
    max_tool_rounds: int = 5

    @field_validator("anthropic_api_key")
    @classmethod
    def _key_required(cls, v: str) -> str:
        if not v or not v.strip() or v.strip().startswith("sk-ant-..."):
            raise ValueError(
                "ANTHROPIC_API_KEY is required. Copy .env.example to .env and set a real key."
            )
        return v.strip()


@lru_cache
def get_settings() -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    return Settings()  # type: ignore[call-arg]
