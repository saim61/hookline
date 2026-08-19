import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx

from hookline.delivery.signing import build_headers
from hookline.models.delivery_attempt import MAX_STORED_RESPONSE_BYTES

# 408 Request Timeout, 425 Too Early, 429 Too Many Requests. Every other 4xx says the
# request itself is wrong - bad path, bad auth, payload rejected - and sending the exact
# same bytes again cannot change the answer. Retrying those five times just delays the
# dead letter by an hour while burning the receiver's error budget. 5xx and transport
# failures are the opposite: the request was fine, the server was not, so retry.
RETRYABLE_CLIENT_ERRORS = frozenset({408, 425, 429})


@dataclass(frozen=True, slots=True)
class AttemptOutcome:
    succeeded: bool
    retryable: bool
    status_code: int | None
    response_body: str | None
    error: str | None
    duration_ms: int


def build_body(
    event_id: UUID, event_type: str, created_at: datetime, payload: dict[str, Any]
) -> bytes:
    """Serialise the envelope once. These exact bytes are both signed and sent.

    Signing a re-serialised copy is a classic way to ship a service whose signatures
    never verify: key order or separator spacing differs by a byte and the HMAC changes
    completely.
    """
    envelope = {
        "id": str(event_id),
        "type": event_type,
        "created_at": created_at.isoformat(),
        "data": payload,
    }
    return json.dumps(envelope, separators=(",", ":")).encode()


def _truncate(text: str) -> str:
    encoded = text.encode()
    if len(encoded) <= MAX_STORED_RESPONSE_BYTES:
        return text
    return encoded[:MAX_STORED_RESPONSE_BYTES].decode(errors="ignore") + "... [truncated]"


class DeliveryClient:
    """POSTs a signed webhook and reports what happened. Raises nothing."""

    def __init__(self, http: httpx.AsyncClient, user_agent: str) -> None:
        self._http = http
        self._user_agent = user_agent

    async def deliver(
        self,
        *,
        url: str,
        signing_secret: str,
        delivery_id: UUID,
        body: bytes,
        timestamp: int | None = None,
    ) -> AttemptOutcome:
        headers = build_headers(
            secret=signing_secret,
            # The delivery id, not the event id: it is unique per destination and stable
            # across retries, so a receiver deduplicating on webhook-id gets exactly-once
            # processing even when the network makes us send the same thing twice.
            message_id=str(delivery_id),
            timestamp=timestamp if timestamp is not None else int(time.time()),
            body=body,
            user_agent=self._user_agent,
        )

        started = time.perf_counter()
        try:
            response = await self._http.post(url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            # Timeout, DNS failure, connection refused, TLS error. No response exists,
            # so status_code stays NULL and the reason goes in `error`.
            return AttemptOutcome(
                succeeded=False,
                retryable=True,
                status_code=None,
                response_body=None,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=_elapsed_ms(started),
            )

        duration_ms = _elapsed_ms(started)
        code = response.status_code
        if 200 <= code < 300:
            return AttemptOutcome(
                succeeded=True,
                retryable=False,
                status_code=code,
                response_body=_truncate(response.text),
                error=None,
                duration_ms=duration_ms,
            )

        retryable = code >= 500 or code in RETRYABLE_CLIENT_ERRORS
        return AttemptOutcome(
            succeeded=False,
            retryable=retryable,
            status_code=code,
            response_body=_truncate(response.text),
            error=f"endpoint returned {code}",
            duration_ms=duration_ms,
        )


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
