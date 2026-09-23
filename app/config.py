from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    app_name: str = "AthenAI Document-to-Deal Pilot"
    environment: Literal["development", "production", "test"] = "development"
    redis_url: str = "redis://localhost:6379/0"
    upload_dir: str = "./data/uploads"
    # NOTE: kept as a tuple (not a list) so it's hashable / has a sane default repr.
    allowed_extensions: tuple[str, ...] = (".pdf", ".docx", ".txt")
    max_file_size_mb: int = 10
    idempotency_ttl_seconds: int = 60 * 60 * 24
    chunk_char_threshold: int = 6000
    chunk_size_chars: int = 4000
    chunk_overlap_chars: int = 200

    llm_provider: Literal["mock", "openai", "deepseek"] = "mock"

    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"

    deepseek_api_key: str | None = None
    deepseek_model: str = "deepseek-chat"
    deepseek_base_url: str = "https://api.deepseek.com"

    llm_max_retries: int = 2
    llm_timeout_seconds: int = 60
    celery_task_always_eager: bool = False
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
