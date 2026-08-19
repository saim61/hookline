"""HMAC signing for outgoing webhooks.

The receiver's problem: an HTTP endpoint that accepts order notifications is a public
URL, and anyone who guesses it can POST a fake order. The signature proves the request
came from someone holding the endpoint's signing secret.

Scheme, closely following Svix's so existing receiver libraries are familiar:

    webhook-id         the delivery id, stable across retries of the same delivery
    webhook-timestamp  unix seconds, signed so it cannot be altered
    webhook-signature  "v1,<base64 hmac-sha256>"

The signed string is `{id}.{timestamp}.{body}` over the exact bytes on the wire.
"""

import base64
import hashlib
import hmac

SECRET_PREFIX = "whsec_"
SIGNATURE_VERSION = "v1"

# Requests older than this are rejected by a correct receiver. Signing the timestamp is
# what makes replay attacks detectable: without it, a captured request stays valid for
# ever because the signature over the body never expires.
DEFAULT_TOLERANCE_SECONDS = 300


def _key(secret: str) -> bytes:
    """Key material from a `whsec_`-prefixed secret.

    The prefix is a human-facing label - it identifies the kind of credential in logs
    and error reports the way Stripe's `sk_live_` does - and is not part of the key.
    """
    return secret.removeprefix(SECRET_PREFIX).encode()


def signed_content(message_id: str, timestamp: int, body: bytes) -> bytes:
    return f"{message_id}.{timestamp}.".encode() + body


def sign(secret: str, message_id: str, timestamp: int, body: bytes) -> str:
    """Return the `webhook-signature` header value.

    Versioned so a second scheme can be introduced later, and so a rotation period can
    send two signatures space-separated ("v1,<old> v1,<new>") with the receiver
    accepting either.
    """
    digest = hmac.new(_key(secret), signed_content(message_id, timestamp, body), hashlib.sha256)
    return f"{SIGNATURE_VERSION},{base64.b64encode(digest.digest()).decode()}"


def build_headers(
    secret: str,
    message_id: str,
    timestamp: int,
    body: bytes,
    user_agent: str,
) -> dict[str, str]:
    return {
        "content-type": "application/json",
        "user-agent": user_agent,
        "webhook-id": message_id,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": sign(secret, message_id, timestamp, body),
    }


def verify(
    secret: str,
    message_id: str,
    timestamp: int,
    body: bytes,
    header_value: str,
    now: int,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> bool:
    """Receiver-side verification. Used by the test suite and documented in the README.

    `hmac.compare_digest` rather than `==`: string equality returns as soon as it hits a
    differing byte, so how long it takes leaks how much of the prefix was correct. An
    attacker can walk a forged signature out one byte at a time from that. compare_digest
    always examines every byte.
    """
    if abs(now - timestamp) > tolerance_seconds:
        return False

    expected = sign(secret, message_id, timestamp, body)
    # Space-separated list supports secret rotation: any one match is enough.
    return any(hmac.compare_digest(expected, candidate) for candidate in header_value.split(" "))
