from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="HOOKLINE_",
        extra="ignore",
    )

    app_name: str = "hookline"
    debug: bool = False
    database_url: str = "postgresql+asyncpg://hookline:hookline@localhost:5432/hookline"
    max_delivery_attempts: int = 5
    delivery_timeout_seconds: float = 10.0


@lru_cache
def get_settings() -> Settings:
    return Settings()