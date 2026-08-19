"""API key generation and hashing.

Keys are shown once, at creation, and only a hash is stored - the same reason
`signing_secret` is only returned from the POST that creates an endpoint. A database dump
must not be enough to call the API.

**Why SHA-256 and not bcrypt or argon2.** Slow hashes exist to make brute force expensive
against *low-entropy* secrets: humans pick "hunter2", so each guess has to cost
milliseconds. An API key here is 256 bits from `secrets.token_urlsafe(32)`, and no amount
of compute enumerates that space. Meanwhile a slow hash would run on *every authenticated
request*, which turns the login-hardening measure into a self-inflicted denial of service -
argon2 at 100ms per request caps the API at ten requests per second per core. Fast hash
over a high-entropy secret is the correct trade, and it is what Stripe, GitHub and
friends do for tokens.
"""

import hashlib
import secrets

KEY_PREFIX = "hl_"

# 32 bytes -> 43 url-safe characters, 256 bits of entropy.
SECRET_BYTES = 32

# How much of the key is stored in the clear. Enough for a human to recognise which key a
# log line or a dashboard row refers to, far too little to guess the rest.
DISPLAY_PREFIX_LENGTH = len(KEY_PREFIX) + 8


def generate_key() -> tuple[str, str, str]:
    """Return (token, display_prefix, key_hash).

    The token is the only time the full value exists anywhere. Callers must hand it to the
    user and then forget it.
    """
    token = f"{KEY_PREFIX}{secrets.token_urlsafe(SECRET_BYTES)}"
    return token, token[:DISPLAY_PREFIX_LENGTH], hash_key(token)


def hash_key(token: str) -> str:
    """Lookup key for a presented token.

    Deterministic and unsalted on purpose: authentication needs to find the row *by* the
    presented token, and a per-row salt would mean hashing the candidate against every
    stored key in turn - a full table scan on every request. A unique index on this value
    makes it one indexed lookup instead. Salts protect low-entropy secrets against
    precomputation; there is no rainbow table for 256-bit random strings.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def looks_like_key(value: str) -> bool:
    """Cheap shape check, so obviously malformed input never reaches the database."""
    return value.startswith(KEY_PREFIX) and len(value) > DISPLAY_PREFIX_LENGTH
