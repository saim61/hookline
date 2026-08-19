from functools import lru_cache
from typing import Literal

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
    # "redis" shares breaker state across every worker; "memory" keeps it per process,
    # which needs no Redis but lets each worker learn the same endpoint is down
    # independently.
    circuit_breaker_backend: Literal["redis", "memory"] = "redis"

    # --- auth -------------------------------------------------------------------
    # Off only for local experimentation and for the pre-auth acceptance scripts. When
    # false every request is treated as holding the admin scope.
    auth_enabled: bool = True
    # How far a signed inbound request's timestamp may be from ours. Wide enough for
    # ordinary clock skew, narrow enough that a captured request stops working.
    inbound_signature_tolerance_seconds: int = Field(default=300, ge=1)

    # --- redis ------------------------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"

    # --- rate limiting ----------------------------------------------------------
    rate_limit_enabled: bool = True
    # Token bucket: `capacity` requests may arrive at once, refilling at
    # `refill_per_second`. Capacity is the burst allowance, refill is the sustained
    # rate - the two together are what let a caller send a spike of 100 and still be
    # held to 20/s on average.
    rate_limit_capacity: int = Field(default=100, ge=1)
    rate_limit_refill_per_second: float = Field(default=20.0, gt=0)

    # Outbound, per destination endpoint, shared across every worker. The inbound limit
    # protects Hookline; this one protects the customer's server, which is the side that
    # did not sign up for whatever traffic spike just happened.
    delivery_rate_limit_enabled: bool = True
    delivery_rate_limit_capacity: int = Field(default=20, ge=1)
    delivery_rate_limit_per_second: float = Field(default=10.0, gt=0)

    # --- caching ----------------------------------------------------------------
    # Subscriber lookups are invalidated explicitly whenever an endpoint changes, so
    # this TTL only bounds how long a stale entry can survive a missed invalidation
    # (a crash between the write and the delete). Short enough to self-heal.
    subscriber_cache_ttl_seconds: int = Field(default=30, ge=1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
