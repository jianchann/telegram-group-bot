"""Process-environment configuration with credential-safe validation."""

import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, StrictInt, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", hide_input_in_errors=True)

    env: str = "development"
    database_url: SecretStr
    telegram_bot_token: SecretStr
    telegram_webhook_secret: SecretStr
    allowed_telegram_chat_ids: frozenset[StrictInt]
    gcp_project_id: str = Field(min_length=1)
    gcp_location: str = Field(default="global", min_length=1)
    gemini_model: str = Field(default="gemini-3.5-flash-lite", min_length=1)
    default_chat_timezone: str = "Asia/Manila"
    max_recent_messages: int = Field(default=30, ge=1, le=200)
    max_memory_results: int = Field(default=20, ge=1, le=100)
    max_url_context_urls: int = Field(default=5, ge=1, le=20)
    auto_memory_enabled: bool = True
    max_context_chars: int = Field(default=24000, ge=256, le=100000)
    max_ai_output_tokens: int = Field(default=1024, ge=64, le=8192)
    max_ai_turns: int = Field(default=4, ge=2, le=8)
    max_ai_tool_calls: int = Field(default=8, ge=1, le=32)
    max_media_bytes: int = Field(default=10_000_000, ge=1, le=20_000_000)
    summary_message_threshold: int = Field(default=100, ge=1, le=1000)
    summary_char_threshold: int = Field(default=40_000, ge=1000, le=1_000_000)
    summary_max_input_chars: int = Field(default=20_000, ge=1000, le=100_000)
    summary_max_output_tokens: int = Field(default=512, ge=64, le=2048)
    summary_timeout_seconds: float = Field(default=10, gt=0, le=30)
    ai_user_requests_per_minute: int = Field(default=5, ge=1, le=100)
    ai_chat_requests_per_minute: int = Field(default=20, ge=1, le=1000)
    gemini_timeout_seconds: float = Field(default=20, gt=0, le=60)
    telegram_timeout_seconds: float = Field(default=10, gt=0, le=30)
    webhook_timeout_seconds: float = Field(default=55, gt=0, le=120)

    @field_validator("database_url")
    @classmethod
    def postgres_only(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
            if url.drivername not in {"postgresql", "postgresql+psycopg"}:
                raise ValueError
            if not url.host or not url.database:
                raise ValueError
        except Exception:
            raise ValueError("DATABASE_URL must be a PostgreSQL URL using psycopg") from None
        return value

    @field_validator("telegram_bot_token")
    @classmethod
    def valid_token(cls, value: SecretStr) -> SecretStr:
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", value.get_secret_value()):
            raise ValueError("TELEGRAM_BOT_TOKEN must be a BotFather token")
        return value

    @field_validator("telegram_webhook_secret")
    @classmethod
    def valid_webhook_secret(cls, value: SecretStr) -> SecretStr:
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", value.get_secret_value()):
            raise ValueError("Webhook secret must contain 16–256 letters, digits, '_' or '-'")
        return value

    @field_validator("allowed_telegram_chat_ids")
    @classmethod
    def nonempty_groups(cls, value: frozenset[int]) -> frozenset[int]:
        if not value or any(chat_id >= 0 for chat_id in value):
            raise ValueError("Configure at least one negative Telegram group chat ID")
        return value

    @field_validator("gcp_project_id", "gcp_location", "gemini_model")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Configuration value must not be blank")
        return value.strip()

    @field_validator("default_chat_timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("DEFAULT_CHAT_TIMEZONE must be an IANA timezone") from None
        return value

    @model_validator(mode="after")
    def sufficient_request_timeout(self) -> "Settings":
        if self.webhook_timeout_seconds <= (
            self.gemini_timeout_seconds
            + self.summary_timeout_seconds
            + self.telegram_timeout_seconds
        ):
            raise ValueError("Webhook timeout must cover Gemini, summary, and Telegram timeouts")
        return self
