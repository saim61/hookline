"""Dashboard pages.

Server-rendered Jinja2, enhanced with HTMX. **Every action also works as a plain form
submit**: HTMX only swaps a fragment instead of reloading the page, so the dashboard is
fully usable with JavaScript disabled or the CDN unreachable. Progressive enhancement
rather than a single-page app - the alternative is a build step and a second data model for
something whose entire job is showing rows from a table.

The one HTMX-specific bit of server logic: a request carrying `HX-Request` gets the
fragment, anything else gets the whole page. That is the same handler either way, so the
two paths cannot drift.
"""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from hookline.api.deps import (
    ApiKeyRepoDep,
    DeliveryRepoDep,
    EndpointRepoDep,
    EventRepoDep,
    RedisDep,
    SessionDep,
    SettingsDep,
)
from hookline.auth import keys
from hookline.auth.scopes import Scope
from hookline.dashboard import session as session_store
from hookline.dashboard.deps import Viewer, require_csrf
from hookline.enums import DeliveryStatus
from hookline.observability.logging import get_logger
from hookline.paths import TEMPLATES_DIR
from hookline.repositories.api_key import ApiKeyRepository

log = get_logger("hookline.dashboard")

router = APIRouter(prefix="/dashboard", tags=["dashboard"], include_in_schema=False)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Available to every template, so expiry can be rendered without each handler passing a
# clock in. Timezone-aware, since the columns it is compared against are.
templates.env.globals["now"] = lambda: datetime.now(UTC)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _render(
    request: Request,
    page: str,
    fragment: str,
    context: dict[str, object],
) -> HTMLResponse:
    """One handler, two renderings.

    HTMX asks for the fragment; a browser without it gets the full page wrapping the same
    fragment. Rendering both from one context is what stops the enhanced and unenhanced
    paths drifting apart.
    """
    template = fragment if _is_htmx(request) else page
    return templates.TemplateResponse(request, template, context)


# --------------------------------------------------------------------- login


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: Annotated[str, Query()] = "/dashboard") -> Response:
    return templates.TemplateResponse(request, "login.html", {"next": next, "error": None})


@router.post("/login")
async def login(
    request: Request,
    db: SessionDep,
    redis: RedisDep,
    api_key: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/dashboard",
) -> Response:
    """Exchange an API key for a session cookie.

    A deliberately vague error on failure: "no such key" and "that key is revoked" both
    read as invalid, for the same reason the API returns an identical 401 for both.
    """
    invalid = templates.TemplateResponse(
        request,
        "login.html",
        {"next": next, "error": "That key was not accepted."},
        status_code=status.HTTP_401_UNAUTHORIZED,
    )

    token = api_key.strip()
    if not keys.looks_like_key(token):
        return invalid

    found = await ApiKeyRepository(db).get_by_hash(keys.hash_key(token))
    if found is None or not found.is_usable(datetime.now(UTC)):
        return invalid

    session_id = await session_store.create(redis, found.id)
    # Only redirect to our own paths: an open redirect here would let a phishing link send
    # someone off-site immediately after they authenticated.
    destination = next if next.startswith("/dashboard") else "/dashboard"
    response = RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        session_store.COOKIE_NAME,
        session_id,
        max_age=session_store.SESSION_TTL_SECONDS,
        # httponly: script cannot read it, so an XSS bug cannot exfiltrate the session.
        httponly=True,
        # strict: the cookie is not sent on cross-site requests at all, which is the first
        # line of CSRF defence. The token in each form is the second.
        samesite="strict",
        # Set secure in production, behind TLS. Left off here so it works over plain HTTP
        # on localhost, where the browser would otherwise silently drop it.
        secure=False,
    )
    log.info("dashboard login", key_id=str(found.id), key_name=found.name)
    return response


@router.post("/logout")
async def logout(request: Request, redis: RedisDep) -> Response:
    await session_store.destroy(redis, request.cookies.get(session_store.COOKIE_NAME))
    response = RedirectResponse("/dashboard/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(session_store.COOKIE_NAME)
    return response


# --------------------------------------------------------------------- overview


@router.get("", response_class=HTMLResponse)
async def overview(
    request: Request,
    viewer: Viewer,
    deliveries: DeliveryRepoDep,
    events: EventRepoDep,
    endpoints: EndpointRepoDep,
    settings: SettingsDep,
) -> Response:
    principal, csrf = viewer
    counts = await deliveries.count_by_status()
    recent = await events.list_recent_with_summary(limit=10)

    return _render(
        request,
        "overview.html",
        "partials/overview_body.html",
        {
            "principal": principal,
            "csrf_token": csrf,
            "counts": counts,
            "totals": {
                "events": await events.count(),
                "endpoints": await endpoints.count(),
                "deliveries": sum(counts.values()),
            },
            "recent": recent,
            "settings": settings,
            "active": "overview",
        },
    )


# --------------------------------------------------------------------- events


@router.get("/events", response_class=HTMLResponse)
async def event_log(
    request: Request,
    viewer: Viewer,
    events: EventRepoDep,
    event_type: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Response:
    principal, csrf = viewer
    if not principal.can(Scope.EVENTS_READ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="this key cannot read events")

    rows = await events.list_recent_with_summary(limit=limit, offset=offset, event_type=event_type)
    return _render(
        request,
        "events.html",
        "partials/event_rows.html",
        {
            "principal": principal,
            "csrf_token": csrf,
            "rows": rows,
            "event_type": event_type or "",
            "limit": limit,
            "offset": offset,
            "active": "events",
        },
    )


@router.get("/events/{event_id}", response_class=HTMLResponse)
async def event_detail(
    request: Request,
    event_id: UUID,
    viewer: Viewer,
    events: EventRepoDep,
    deliveries: DeliveryRepoDep,
    endpoints: EndpointRepoDep,
) -> Response:
    import json

    principal, csrf = viewer
    if not principal.can(Scope.EVENTS_READ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="this key cannot read events")

    event = await events.get(event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="event not found")

    rows = await deliveries.list_for_event(event_id)
    detail = []
    for delivery in rows:
        endpoint = await endpoints.get(delivery.endpoint_id)
        detail.append(
            {
                "delivery": delivery,
                "url": endpoint.url if endpoint else "(endpoint deleted)",
                "attempts": await deliveries.list_attempts(delivery.id),
            }
        )

    return templates.TemplateResponse(
        request,
        "event_detail.html",
        {
            "principal": principal,
            "csrf_token": csrf,
            "event": event,
            "payload_json": json.dumps(event.payload, indent=2, sort_keys=True),
            "rows": detail,
            "active": "events",
        },
    )


# --------------------------------------------------------------------- deliveries


@router.get("/deliveries", response_class=HTMLResponse)
async def delivery_list(
    request: Request,
    viewer: Viewer,
    deliveries: DeliveryRepoDep,
    delivery_status: Annotated[str, Query(alias="status")] = DeliveryStatus.DEAD.value,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Response:
    principal, csrf = viewer
    if not principal.can(Scope.DELIVERIES_READ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="this key cannot read deliveries")

    chosen: DeliveryStatus | None
    if delivery_status == "all":
        chosen = None
    else:
        try:
            chosen = DeliveryStatus(delivery_status)
        except ValueError as exc:
            # 422 by number: starlette deprecated HTTP_422_UNPROCESSABLE_ENTITY in favour of
            # HTTP_422_UNPROCESSABLE_CONTENT, and reading the old name emits a warning from
            # inside the handler - which under `filterwarnings = error` turns this 422 into
            # a 500. The number is stable in a way the constant's name is not.
            raise HTTPException(422, detail=f"unknown status {delivery_status!r}") from exc

    rows = await deliveries.list_with_endpoint(chosen, limit=limit, offset=offset)
    enriched = [
        {
            "delivery": delivery,
            "url": url,
            "last_attempt": await deliveries.latest_attempt(delivery.id),
        }
        for delivery, url in rows
    ]

    return _render(
        request,
        "deliveries.html",
        "partials/delivery_rows.html",
        {
            "principal": principal,
            "csrf_token": csrf,
            "rows": enriched,
            "status": delivery_status,
            "statuses": ["all", *[s.value for s in DeliveryStatus]],
            "counts": await deliveries.count_by_status(),
            "limit": limit,
            "offset": offset,
            "can_replay": principal.can(Scope.DELIVERIES_WRITE),
            "active": "deliveries",
        },
    )


@router.post("/deliveries/{delivery_id}/replay")
async def replay(
    request: Request,
    delivery_id: UUID,
    viewer: Viewer,
    deliveries: DeliveryRepoDep,
    settings: SettingsDep,
    csrf_token: Annotated[str | None, Form()] = None,
) -> Response:
    """Requeue one dead delivery.

    HTMX swaps just this row; without it the browser follows the redirect back to the list.
    Both end up showing the same updated state.
    """
    principal, expected_csrf = viewer
    require_csrf(csrf_token, expected_csrf)

    if not principal.can(Scope.DELIVERIES_WRITE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="this key cannot replay deliveries")

    delivery = await deliveries.replay(delivery_id, extra_attempts=settings.max_delivery_attempts)
    if delivery is None:
        existing = await deliveries.get(delivery_id)
        if existing is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="delivery not found")
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"only dead deliveries can be replayed, this one is {existing.status}",
        )

    log.info(
        "delivery replayed from dashboard",
        delivery_id=str(delivery_id),
        key_id=str(principal.key_id),
    )

    if _is_htmx(request):
        return templates.TemplateResponse(
            request,
            "partials/delivery_row.html",
            {
                "row": {
                    "delivery": delivery,
                    "url": "",
                    "last_attempt": await deliveries.latest_attempt(delivery.id),
                },
                "csrf_token": expected_csrf,
                "can_replay": True,
                "just_replayed": True,
            },
        )
    return RedirectResponse("/dashboard/deliveries", status_code=status.HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------- endpoints


@router.get("/endpoints", response_class=HTMLResponse)
async def endpoint_list(request: Request, viewer: Viewer, endpoints: EndpointRepoDep) -> Response:
    principal, csrf = viewer
    if not principal.can(Scope.ENDPOINTS_READ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="this key cannot read endpoints")

    return _render(
        request,
        "endpoints.html",
        "partials/endpoint_rows.html",
        {
            "principal": principal,
            "csrf_token": csrf,
            "rows": await endpoints.list_all(),
            "active": "endpoints",
        },
    )


# --------------------------------------------------------------------- api keys


@router.get("/api-keys", response_class=HTMLResponse)
async def api_key_list(request: Request, viewer: Viewer, api_keys: ApiKeyRepoDep) -> Response:
    principal, csrf = viewer
    if not principal.can(Scope.ADMIN):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="this page requires the admin scope")

    return _render(
        request,
        "api_keys.html",
        "partials/api_key_rows.html",
        {
            "principal": principal,
            "csrf_token": csrf,
            "rows": await api_keys.list_all(),
            "active": "api-keys",
        },
    )
