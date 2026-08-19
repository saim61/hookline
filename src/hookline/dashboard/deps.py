"""Dashboard authentication: cookie session in, Principal out."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from hookline.api.deps import RedisDep, SessionDep
from hookline.auth.dependencies import Principal
from hookline.dashboard import session as session_store
from hookline.repositories.api_key import ApiKeyRepository


class NotLoggedInError(Exception):
    """Raised so an exception handler can redirect, which a dependency cannot do.

    Returning a RedirectResponse from a dependency does not work - FastAPI treats the
    return value as the injected parameter, not as the response - so the redirect has to
    happen at the exception-handler layer.
    """


async def get_viewer(request: Request, redis: RedisDep, db: SessionDep) -> tuple[Principal, str]:
    """The logged-in principal plus this session's CSRF token.

    The key is re-read on every request rather than trusted from the session, so revoking
    or expiring a key logs the holder out of the dashboard immediately instead of at the
    end of an eight hour session.
    """
    stored = await session_store.resolve(redis, request.cookies.get(session_store.COOKIE_NAME))
    if stored is None:
        raise NotLoggedInError

    api_key = await ApiKeyRepository(db).get(stored.key_id)
    if api_key is None or not api_key.is_usable(datetime.now(UTC)):
        raise NotLoggedInError

    principal = Principal(key_id=api_key.id, name=api_key.name, granted=frozenset(api_key.scopes))
    return principal, stored.csrf_token


Viewer = Annotated[tuple[Principal, str], Depends(get_viewer)]


def login_redirect(request: Request) -> RedirectResponse:
    """Send an unauthenticated viewer to the login form, remembering where they wanted to go."""
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    suffix = f"?next={target}" if target != "/dashboard/login" else ""
    return RedirectResponse(f"/dashboard/login{suffix}", status_code=status.HTTP_303_SEE_OTHER)


def require_csrf(submitted: str | None, expected: str) -> None:
    """Guard state-changing forms.

    SameSite=Strict on the session cookie already stops most cross-site submits, but a
    token does not depend on the browser honouring a cookie attribute - and replay is
    exactly the kind of action worth two defences.
    """
    import hmac

    if not submitted or not hmac.compare_digest(submitted, expected):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="invalid or missing CSRF token")
