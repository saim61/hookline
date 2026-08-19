from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from hookline.auth.dependencies import Principal, PrincipalDep
from hookline.cache.client import get_redis
from hookline.cache.ratelimit import TokenBucketLimiter
from hookline.cache.subscribers import SubscriberCache
from hookline.config import Settings, get_settings
from hookline.db.session import get_session
from hookline.repositories.api_key import ApiKeyRepository
from hookline.repositories.delivery import DeliveryRepository
from hookline.repositories.endpoint import EndpointRepository
from hookline.repositories.event import EventRepository
from hookline.services.event_ingest import EventIngestService

SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
RedisDep = Annotated[Redis, Depends(get_redis)]


# --------------------------------------------------------------------- repositories


async def get_endpoint_repo(session: SessionDep) -> EndpointRepository:
    return EndpointRepository(session)


async def get_event_repo(session: SessionDep) -> EventRepository:
    return EventRepository(session)


async def get_delivery_repo(session: SessionDep) -> DeliveryRepository:
    return DeliveryRepository(session)


async def get_api_key_repo(session: SessionDep) -> ApiKeyRepository:
    return ApiKeyRepository(session)


EndpointRepoDep = Annotated[EndpointRepository, Depends(get_endpoint_repo)]
EventRepoDep = Annotated[EventRepository, Depends(get_event_repo)]
DeliveryRepoDep = Annotated[DeliveryRepository, Depends(get_delivery_repo)]
ApiKeyRepoDep = Annotated[ApiKeyRepository, Depends(get_api_key_repo)]

# Alias kept so the endpoints routes still read as `repo: RepoDep`. Now that several
# repositories exist, new code should use the explicit name.
RepoDep = EndpointRepoDep


# --------------------------------------------------------------------- cache


async def get_subscriber_cache(redis: RedisDep, settings: SettingsDep) -> SubscriberCache:
    return SubscriberCache(redis, ttl_seconds=settings.subscriber_cache_ttl_seconds)


SubscriberCacheDep = Annotated[SubscriberCache, Depends(get_subscriber_cache)]


# --------------------------------------------------------------------- services


async def get_event_ingest_service(
    events: EventRepoDep,
    endpoints: EndpointRepoDep,
    deliveries: DeliveryRepoDep,
    settings: SettingsDep,
    subscriber_cache: SubscriberCacheDep,
) -> EventIngestService:
    return EventIngestService(
        events=events,
        endpoints=endpoints,
        deliveries=deliveries,
        max_delivery_attempts=settings.max_delivery_attempts,
        subscriber_cache=subscriber_cache,
    )


EventIngestDep = Annotated[EventIngestService, Depends(get_event_ingest_service)]


# --------------------------------------------------------------------- rate limiting


def rate_limit_identity(principal: Principal, request: Request) -> str:
    """Who the limit applies to.

    The API key, when there is one. IP is a poor identity for a limit: it is spoofable,
    and it is shared - one office behind one NAT gets one bucket between them, so a busy
    neighbour throttles everyone. The key identifies the actual caller.

    Falls back to IP only when auth is disabled, since then there is no key to key on.
    """
    if principal.authenticated:
        return f"key:{principal.key_id}"
    if request.client is None:
        return "anonymous"
    return f"ip:{request.client.host}"


async def enforce_rate_limit(
    request: Request,
    principal: PrincipalDep,
    response_settings: SettingsDep,
    redis: RedisDep,
) -> None:
    """429 when the caller is over budget, with a Retry-After they can act on.

    Attached to write routes rather than globally: throttling reads of the dead letter
    queue while an operator is trying to work through an incident would be actively
    unhelpful, and reads are cheap.
    """
    if not response_settings.rate_limit_enabled:
        return

    limiter = TokenBucketLimiter(
        redis,
        capacity=response_settings.rate_limit_capacity,
        refill_per_second=response_settings.rate_limit_refill_per_second,
    )
    result = await limiter.check(rate_limit_identity(principal, request))
    if result.allowed:
        return

    raise HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        detail="rate limit exceeded",
        # Standard header. Without it a client's only option is to guess, and clients
        # that guess tend to guess "immediately".
        headers={"Retry-After": str(max(1, round(result.retry_after_seconds)))},
    )


RateLimited = Depends(enforce_rate_limit)
