from functools import lru_cache
from pathlib import Path

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError:  # pragma: no cover - only used by the dependency-free static smoke environment
    from pydantic import BaseModel

    class BaseSettings(BaseModel):
        """Fallback allowing SQLite and workflow tests before optional dependencies are installed."""

    def SettingsConfigDict(**kwargs):
        return kwargs


class Settings(BaseSettings):
    run_mode: str = "mock"
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_input_price_per_1m: float | None = None
    llm_output_price_per_1m: float | None = None
    price_currency: str = "CNY"
    database_path: str = "data/supportflow.db"
    checkpoint_path: str = "data/checkpoints.db"
    max_model_calls: int = 8
    max_tool_calls: int = 12
    task_timeout_seconds: int = 120
    model_timeout_seconds: int = 30
    max_model_retries: int = 1
    policy_version: str = "v1"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    def ensure_dirs(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.checkpoint_path).parent.mkdir(parents=True, exist_ok=True)

    @property
    def live_configured(self) -> bool:
        return self.run_mode.lower() == "live" and bool(self.llm_api_key and self.llm_model)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
