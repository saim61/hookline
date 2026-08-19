from functools import lru_cache

from pydantic import Field
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

    # --- delivery ---------------------------------------------------------------
    max_delivery_attempts: int = Field(default=5, ge=1)
    delivery_timeout_seconds: float = Field(default=10.0, gt=0)

    # --- worker loop ------------------------------------------------------------
    # How long to sleep when a poll finds nothing. Short enough that a new event is
    # picked up promptly, long enough that an idle worker is not hammering Postgres.
    worker_poll_interval_seconds: float = Field(default=1.0, gt=0)
    # Rows claimed per poll. Each is delivered concurrently, so this is also the
    # worker's maximum in-flight HTTP requests.
    worker_batch_size: int = Field(default=20, ge=1, le=500)

    # A delivery left in_flight for this long is assumed to belong to a worker that
    # crashed mid-request, and is returned to pending. Must comfortably exceed
    # delivery_timeout_seconds or live requests will be reclaimed underneath a
    # healthy worker, causing double delivery.
    stale_delivery_timeout_seconds: float = Field(default=300.0, gt=0)

    # --- retry backoff ----------------------------------------------------------
    # Delay before attempt N is roughly base * 2^(N-1), capped, then jittered.
    retry_base_delay_seconds: float = Field(default=10.0, gt=0)
    retry_max_delay_seconds: float = Field(default=3600.0, gt=0)

    # --- circuit breaker --------------------------------------------------------
    # Consecutive failures against one endpoint before Hookline stops trying it.
    circuit_breaker_failure_threshold: int = Field(default=5, ge=1)
    # How long the circuit stays open before one probe request is allowed through.
    circuit_breaker_cooldown_seconds: float = Field(default=60.0, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
