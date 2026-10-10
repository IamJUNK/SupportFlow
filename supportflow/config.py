from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    run_mode: Literal["mock", "live"] = "mock"
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_input_price_per_1m: float | None = Field(default=None, ge=0)
    llm_output_price_per_1m: float | None = Field(default=None, ge=0)
    price_currency: str = "CNY"
    database_path: str = "data/supportflow.db"
    checkpoint_path: str = "data/checkpoints.db"
    max_model_calls: int = Field(default=8, ge=1, le=100)
    max_tool_calls: int = Field(default=12, ge=1, le=100)
    task_timeout_seconds: float = Field(default=120, gt=0)
    model_timeout_seconds: float = Field(default=30, gt=0)
    max_model_retries: int = Field(default=1, ge=0, le=5)
    policy_version: str = "v1"
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore", populate_by_name=True)

    @field_validator("llm_base_url", "llm_api_key", "llm_model", "llm_input_price_per_1m", "llm_output_price_per_1m", mode="before")
    @classmethod
    def empty_optional(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    def ensure_dirs(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.checkpoint_path).parent.mkdir(parents=True, exist_ok=True)

    @property
    def live_configured(self) -> bool:
        return bool(self.llm_api_key and self.llm_model)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
