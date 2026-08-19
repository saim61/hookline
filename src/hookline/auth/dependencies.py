"""Authentication and authorisation as FastAPI dependencies."""

import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis

from hookline.auth import keys, scopes
from hookline.auth.scopes import Scope
from hookline.cache.client import get_redis
from hookline.config import Settings, get_settings
from hookline.db.session import get_session
from hookline.delivery import signing
from hookline.repositories.api_key import ApiKeyRepository

log = logging.getLogger("hookline.auth")

# auto_error=False so a missing header reaches our own handler and produces a consistent
# message, rather than FastAPI's generic 403.
_bearer = HTTPBearer(auto_error=False, description="API key as `Bearer hl_...`")


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is making this request. Injected into handlers that need to know."""

    key_id: UUID
    name: str
    granted: frozenset[str]
    # False only for the auth-disabled placeholder. An explicit flag rather than an
    # identity comparison against a module-private sentinel, so callers elsewhere can ask
    # the question without importing internals.
    authenticated: bool = True

    def can(self, scope: Scope) -> bool:
        return scopes.grants(self.granted, scope)


# Used when HOOKLINE_AUTH_ENABLED is false. Holds admin so every route behaves as it
# would for a fully privileged key.
ANONYMOUS = Principal(
    key_id=UUID(int=0),
    name="auth-disabled",
    granted=frozenset({Scope.ADMIN.value}),
    authenticated=False,
)

_UNAUTHENTICATED = HTTPException(
    status.HTTP_401_UNAUTHORIZED,
    detail="missing or invalid API key",
    # Sending the scheme back is what tells a client library how to retry. Note 401 for
    # "we do not know who you are" versus 403 for "we know, and you may not" - conflating
    # them makes a scope problem look like a credential problem and sends people hunting
    # for the wrong bug.
    headers={"WWW-Authenticate": "Bearer"},
)


async def _throttled_touch(redis: Redis, repo: ApiKeyRepository, key_id: UUID) -> None:
    """Record last_used_at at most once a minute per key.

    Writing it on every request would add a row update to the hot path for the sake of a
    reporting field. SET NX EX is the cheapest possible "have we done this recently".
    Failures are ignored entirely: not knowing when a key was last used is never a reason
    to reject a valid request.
    """
    try:
        if await redis.set(f"hookline:touched:{key_id}", "1", ex=60, nx=True):
            await repo.touch(key_id)
    except Exception:
        log.debug("last_used_at update skipped", exc_info=True)


async def get_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[Any, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> Principal:
    if not settings.auth_enabled:
        return ANONYMOUS

    if credentials is None or not keys.looks_like_key(credentials.credentials):
        raise _UNAUTHENTICATED

    repo = ApiKeyRepository(session)
    api_key = await repo.get_by_hash(keys.hash_key(credentials.credentials))
    if api_key is None:
        raise _UNAUTHENTICATED

    now = datetime.now(UTC)
    if not api_key.is_usable(now):
        # Deliberately the same 401 as an unknown key. Distinguishing "revoked" from
        # "never existed" tells an attacker holding a leaked key that it was real.
        raise _UNAUTHENTICATED

    if api_key.inbound_signing_secret is not None:
        await _verify_inbound_signature(request, api_key.inbound_signing_secret, settings)

    await _throttled_touch(redis, repo, api_key.id)
    return Principal(key_id=api_key.id, name=api_key.name, granted=frozenset(api_key.scopes))


async def _verify_inbound_signature(request: Request, secret: str, settings: Settings) -> None:
    """Require a valid HMAC over the request body, the same scheme Hookline sends with.

    Two things this buys over the bearer key alone. The signature proves the caller holds
    the secret without ever putting it on the wire, so an intercepted request cannot be
    reused to send a different one. And because the timestamp is part of the signed
    string, a captured request stops being valid - a bearer token replayed verbatim is
    accepted forever, a signed request is not.
    """
    signature = request.headers.get("webhook-signature")
    timestamp = request.headers.get("webhook-timestamp")
    message_id = request.headers.get("webhook-id")
    if not (signature and timestamp and message_id):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="this key requires signed requests: send webhook-id, "
            "webhook-timestamp and webhook-signature",
        )

    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail="webhook-timestamp must be unix seconds"
        ) from exc

    # Starlette caches the body, so reading it here does not consume it for the handler
    # that parses the JSON afterwards. The signature must cover these exact bytes: any
    # re-serialisation changes the digest.
    body = await request.body()
    if not signing.verify(
        secret=secret,
        message_id=message_id,
        timestamp=ts,
        body=body,
        header_value=signature,
        now=int(time.time()),
        tolerance_seconds=settings.inbound_signature_tolerance_seconds,
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid request signature")


PrincipalDep = Annotated[Principal, Depends(get_principal)]


def require(scope: Scope) -> Callable[[Principal], Coroutine[Any, Any, Principal]]:
    """Dependency factory: 403 unless the principal holds `scope`.

    Returned as a dependency rather than checked inside each handler so the requirement is
    visible in the route declaration - and so it appears in the generated OpenAPI docs
    instead of being buried in a function body.
    """

    async def _check(principal: PrincipalDep) -> Principal:
        if not principal.can(scope):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail=f"this key is missing the required scope: {scope.value}",
            )
        return principal

    return _check


def requires(scope: Scope) -> Any:
    """`dependencies=[requires(Scope.X)]` for routes that need no principal object."""
    return Depends(require(scope))
