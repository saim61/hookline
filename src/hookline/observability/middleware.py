"""Request id, structured access log, and HTTP metrics in one pass."""

import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from hookline.observability import metrics
from hookline.observability.context import REQUEST_ID_HEADER, request_id_var
from hookline.observability.logging import get_logger

log = get_logger("hookline.request")

# Paths that would otherwise dominate the logs and the metrics with no information.
# Prometheus scrapes /metrics every 15 seconds forever; a probe hits /health constantly.
_QUIET_PATHS = frozenset({"/health", "/ready", "/metrics"})


def _route_template(scope: Scope) -> str:
    """The full route pattern, not the concrete path.

    `/api/v1/events/{event_id}` is one metric series; `/api/v1/events/<uuid>` is one
    series per event, which is how a monitoring system gets taken down by the thing it was
    installed to monitor. Starlette puts the matched route on the scope after routing,
    which is why this is read at the end of the request rather than the start.

    The matched route only knows its path *within its own router* - `/endpoints/{id}`,
    with no `/api/v1` - so using it directly would drop the prefix and make a future
    `/api/v2/endpoints` collide with v1 in the same series. The prefix is recovered by
    filling the template's parameters back in, which gives the concrete tail, and
    subtracting its length from the concrete path. Exact, with no guessing about where a
    substring might also appear.
    """
    route = scope.get("route")
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    if not isinstance(template, str):
        # No route matched: a 404 on an arbitrary URL. Collapsing every one of those into
        # a single label is the entire point.
        return "unmatched"

    full_path = str(scope.get("path", ""))
    tail = template
    for name, value in (scope.get("path_params") or {}).items():
        tail = tail.replace("{" + name + "}", str(value))

    if full_path.endswith(tail):
        return full_path[: len(full_path) - len(tail)] + template
    return template


class ObservabilityMiddleware:
    """Raw ASGI rather than BaseHTTPMiddleware.

    BaseHTTPMiddleware wraps the request in an anyio task group, which breaks contextvars
    set inside it - the request id would be invisible to the handler's own log lines,
    which is the one thing this middleware exists to provide.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        # Honour a caller-supplied id so a trace spanning several services shares one, and
        # generate one otherwise. Either way the response carries it, so a user reporting
        # a problem can quote a value that finds the exact request in the logs.
        request_id = headers.get(REQUEST_ID_HEADER.lower()) or uuid.uuid4().hex
        token = request_id_var.set(request_id)

        status_code = 500
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message.setdefault("headers", [])
                message["headers"].append((REQUEST_ID_HEADER.lower().encode(), request_id.encode()))
            await send(message)

        try:
            await self._app(scope, receive, send_wrapper)
        except Exception:
            # Record and log the failure, then re-raise so the app's own handlers still
            # produce the response. Swallowing it here would hide real errors behind a
            # metric.
            duration = time.perf_counter() - started
            self._record(scope, status_code, duration)
            log.exception(
                "request failed",
                method=scope.get("method"),
                path=scope.get("path"),
                duration_ms=round(duration * 1000, 2),
            )
            raise
        else:
            duration = time.perf_counter() - started
            self._record(scope, status_code, duration)
            if scope.get("path") not in _QUIET_PATHS:
                log.info(
                    "request",
                    method=scope.get("method"),
                    path=scope.get("path"),
                    route=_route_template(scope),
                    status=status_code,
                    duration_ms=round(duration * 1000, 2),
                )
        finally:
            request_id_var.reset(token)

    @staticmethod
    def _record(scope: Scope, status_code: int, duration: float) -> None:
        if scope.get("path") in _QUIET_PATHS:
            return
        method = str(scope.get("method", "-"))
        route = _route_template(scope)
        metrics.http_requests.labels(method=method, route=route, status=str(status_code)).inc()
        metrics.http_request_duration.labels(method=method, route=route).observe(duration)
