"""Structured logging.

The difference that matters: `log.info("delivery failed", delivery_id=..., status=503)`
produces a machine-readable object, where `log.info(f"delivery {id} failed with 503")`
produces a sentence someone has to write a regex against later. Once logs are JSON,
"show me every failed delivery to endpoint X in the last hour" is a query rather than an
archaeology project.

Two output modes, chosen by `debug`:

    debug=True   colourised key=value on one line, for reading in a terminal
    debug=False  one JSON object per line, for a log shipper

stdlib `logging` is routed through the same pipeline, so uvicorn's and SQLAlchemy's output
comes out in the same format instead of interleaving two conventions in one stream.
"""

import logging
import sys
from typing import Any

import structlog

from hookline.observability.context import request_id_var


def _add_request_id(
    _logger: Any, _name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Stamp every line with the current request id, if there is one.

    A contextvar rather than a parameter threaded through every call: the id has to reach
    log lines emitted deep inside a repository, and passing it down five layers purely so
    logging can see it would put an observability concern into every signature.
    """
    request_id = request_id_var.get()
    if request_id is not None:
        event_dict["request_id"] = request_id
    return event_dict


def configure_logging(*, debug: bool, service: str) -> None:
    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        _add_request_id,
        structlog.processors.add_log_level,
        # ISO 8601 UTC. Local time in logs is a small cruelty to whoever correlates them
        # with a trace from another timezone at 3am.
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: structlog.types.Processor = (
        structlog.dev.ConsoleRenderer(colors=True) if debug else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[
            *shared,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Everything that uses stdlib logging - uvicorn, sqlalchemy, httpx - is rendered by
    # the same formatter, so one stream has one format.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
        )
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if debug else logging.INFO)

    # Access logs are redundant here: the request middleware already emits one structured
    # line per request with timing, status and request id. Leaving both on doubles the
    # volume and makes the useful one harder to find.
    logging.getLogger("uvicorn.access").handlers.clear()
    logging.getLogger("uvicorn.access").propagate = False

    structlog.contextvars.bind_contextvars(service=service)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
