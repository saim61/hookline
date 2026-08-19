"""Request-scoped context.

A contextvar rather than a parameter, because the request id has to be visible to log
lines emitted several layers down - inside a repository, inside the ingest service - and
threading it through every signature would spread an observability concern across the
whole codebase.

contextvars are the async-correct tool: each task gets its own copy, so two requests
handled concurrently on the same event loop never see each other's id. A module-level
global would leak between them.
"""

from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

REQUEST_ID_HEADER = "X-Request-ID"
