import hashlib

import pytest

from hookline.auth import keys, scopes
from hookline.auth.scopes import Scope


def test_generated_key_shape() -> None:
    token, prefix, digest = keys.generate_key()

    assert token.startswith("hl_")
    # 32 random bytes -> 43 url-safe characters. 256 bits, which is the reason a fast hash
    # is sufficient.
    assert len(token) - len("hl_") == 43
    assert token.startswith(prefix)
    assert len(prefix) == 11
    assert len(digest) == 64


def test_keys_are_unique() -> None:
    assert len({keys.generate_key()[0] for _ in range(200)}) == 200


def test_display_prefix_reveals_almost_nothing() -> None:
    """Enough to identify a key in a log line, nowhere near enough to guess it."""
    token, prefix, _ = keys.generate_key()
    assert len(token) - len(prefix) == 35


def test_hash_is_plain_sha256_of_the_whole_token() -> None:
    """Deterministic and unsalted, on purpose.

    Authentication has to find the row *by* the presented key. A per-row salt would mean
    hashing the candidate against every stored key in turn - a full table scan per request -
    where a unique index on this digest is one lookup. Salts defeat precomputation against
    weak secrets, and there is no rainbow table for 256-bit random strings.
    """
    token, _, digest = keys.generate_key()
    assert digest == hashlib.sha256(token.encode()).hexdigest()
    assert keys.hash_key(token) == digest


def test_a_one_character_difference_changes_the_hash() -> None:
    token, _, digest = keys.generate_key()
    assert keys.hash_key(token[:-1] + ("A" if token[-1] != "A" else "B")) != digest


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("hl_" + "x" * 43, True),
        ("hl_", False),
        ("hl_short", False),
        ("", False),
        ("Bearer hl_xxxxxxxxxxxx", False),
        ("sk_live_something_long_enough_here", False),
    ],
)
def test_shape_check(value: str, expected: bool) -> None:
    """Cheap filter so obviously malformed input never reaches the database."""
    assert keys.looks_like_key(value) is expected


# --------------------------------------------------------------------- scopes


def test_exact_scope_grants_itself() -> None:
    assert scopes.grants(frozenset({Scope.EVENTS_WRITE.value}), Scope.EVENTS_WRITE)


def test_read_does_not_imply_write() -> None:
    assert not scopes.grants(frozenset({Scope.EVENTS_READ.value}), Scope.EVENTS_WRITE)


def test_write_does_not_imply_read() -> None:
    """Deliberately not hierarchical.

    An ingest-only key should not be able to read the event log, which may contain other
    customers' payloads. Implying read from write would hand that over silently.
    """
    assert not scopes.grants(frozenset({Scope.EVENTS_WRITE.value}), Scope.EVENTS_READ)


def test_scopes_do_not_leak_across_resources() -> None:
    held = frozenset({Scope.EVENTS_WRITE.value})
    assert not scopes.grants(held, Scope.ENDPOINTS_READ)
    assert not scopes.grants(held, Scope.DELIVERIES_WRITE)


@pytest.mark.parametrize("required", list(Scope))
def test_admin_is_a_wildcard(required: Scope) -> None:
    """Checked at authorisation time, not expanded at creation.

    A key created today therefore covers a scope added next month, instead of quietly
    being under-privileged after every release.
    """
    assert scopes.grants(frozenset({Scope.ADMIN.value}), required)


def test_empty_scope_set_grants_nothing() -> None:
    assert all(not scopes.grants(frozenset(), s) for s in Scope)


def test_unknown_scope_strings_are_rejected() -> None:
    assert scopes.is_valid("events:write")
    assert not scopes.is_valid("events:destroy")
    assert not scopes.is_valid("")
    assert not scopes.is_valid("ADMIN")
