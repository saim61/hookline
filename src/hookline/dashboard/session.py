"""Cookie sessions for the dashboard.

The API authenticates with `Authorization: Bearer hl_...`, which a browser cannot send by
following a link. So the dashboard trades a key for a session once, at login.

The session id is random and opaque; the **key itself is never put in the browser**. Redis
holds `session id -> key id`, so a stolen cookie is scoped to one session that can be
revoked and expires on its own, rather than being the credential itself. That is also why
this is worth 40 lines instead of just storing the key in the cookie.

If Redis is unavailable the dashboard cannot log anyone in - unlike everything else that
touches Redis, this one cannot fail open, because failing open here means letting people
in. Read-only pages for an already-valid session degrade the same way the API does.
"""

import secrets
from dataclasses import dataclass
from uuid import UUID

from redis.asyncio import Redis

from hookline.observability.logging import get_logger

log = get_logger("hookline.dashboard")

COOKIE_NAME = "hookline_session"
SESSION_TTL_SECONDS = 8 * 3600


@dataclass(frozen=True, slots=True)
class Session:
    key_id: UUID
    # Bound to the session and required on every state-changing form. SameSite=Strict
    # already blocks most cross-site submits, but a token costs nothing and does not
    # depend on the browser honouring a cookie attribute.
    csrf_token: str


def _key(session_id: str) -> str:
    return f"hookline:session:{session_id}"


async def create(redis: Redis, key_id: UUID) -> str:
    session_id = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(24)
    await redis.hset(_key(session_id), mapping={"key_id": str(key_id), "csrf": csrf_token})
    await redis.expire(_key(session_id), SESSION_TTL_SECONDS)
    return session_id


async def resolve(redis: Redis, session_id: str | None) -> Session | None:
    if not session_id:
        return None
    try:
        data = await redis.hgetall(_key(session_id))
    except Exception:
        log.warning("session lookup failed", exc_info=True)
        return None
    if not data:
        return None

    # Sliding expiry: an operator working through a DLQ for an hour should not be logged
    # out mid-incident.
    try:
        await redis.expire(_key(session_id), SESSION_TTL_SECONDS)
    except Exception:
        pass

    # decode_responses=True is a pool setting the type system cannot see, so redis-py
    # types every read as `bytes | str`. Normalising beats a cast, which would be silently
    # wrong if that setting ever changed.
    def as_text(value: bytes | str) -> str:
        return value.decode() if isinstance(value, bytes) else value

    try:
        return Session(key_id=UUID(as_text(data["key_id"])), csrf_token=as_text(data["csrf"]))
    except (KeyError, ValueError):
        return None


async def destroy(redis: Redis, session_id: str | None) -> None:
    if session_id:
        try:
            await redis.delete(_key(session_id))
        except Exception:
            log.warning("session delete failed", exc_info=True)
