from functools import lru_cache
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DATABASE_URL: str = "sqlite+aiosqlite:///./local.db"

    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-haiku-4-5-20251001"
    ANTHROPIC_ALLOWED_MODELS: str = (
        "claude-haiku-4-5-20251001,claude-sonnet-4-6,claude-opus-4-7"
    )

    REDDIT_CLIENT_ID: str = ""
    REDDIT_CLIENT_SECRET: str = ""
    REDDIT_USER_AGENT: str = "web:review-collector:v0.1 (by /u/yourname)"

    SCRAPER_USER_AGENT: str = "review-collector-bot/0.1"
    PLAYWRIGHT_ENABLED: bool = False

    DEFAULT_LANGUAGE: str = "en"

    APP_ENV: str = "development"
    LOG_LEVEL: str = "INFO"
    ANALYSIS_BATCH_SIZE: int = 8
    ANALYSIS_CONCURRENCY: int = 4

    @property
    def allowed_models(self) -> List[str]:
        return [m.strip() for m in self.ANTHROPIC_ALLOWED_MODELS.split(",") if m.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
