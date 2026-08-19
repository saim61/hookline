import base64
import hashlib
import hmac

import pytest

from hookline.delivery import signing

SECRET = "whsec_test_secret_material"
MESSAGE_ID = "5e63345d-050b-4636-b88f-d8cc74d4a9df"
TIMESTAMP = 1_787_112_370
BODY = b'{"id":"abc","type":"order.created","data":{"n":1}}'


def test_signature_is_versioned() -> None:
    """The v1 prefix is what makes a future scheme change a non-event.

    Without it there is no way to introduce a new algorithm without breaking every
    receiver on the same day.
    """
    assert signing.sign(SECRET, MESSAGE_ID, TIMESTAMP, BODY).startswith("v1,")


def test_matches_an_independent_implementation() -> None:
    """Computed here from first principles rather than by calling sign() twice.

    A test that compares the function to itself passes even if the scheme is wrong; this
    one fails if the signed string, the key derivation or the encoding changes.
    """
    key = SECRET.removeprefix("whsec_").encode()
    signed = f"{MESSAGE_ID}.{TIMESTAMP}.".encode() + BODY
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()

    assert signing.sign(SECRET, MESSAGE_ID, TIMESTAMP, BODY) == f"v1,{expected}"


def test_prefix_is_not_part_of_the_key() -> None:
    """`whsec_` is a human-facing label, like Stripe's `sk_live_`, not key material."""
    assert signing.sign(SECRET, MESSAGE_ID, TIMESTAMP, BODY) == signing.sign(
        SECRET.removeprefix("whsec_"), MESSAGE_ID, TIMESTAMP, BODY
    )


def test_round_trip_verifies() -> None:
    sig = signing.sign(SECRET, MESSAGE_ID, TIMESTAMP, BODY)
    assert signing.verify(SECRET, MESSAGE_ID, TIMESTAMP, BODY, sig, now=TIMESTAMP)


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("body changed", {"body": BODY + b" "}),
        ("wrong secret", {"secret": "whsec_other"}),
        ("wrong message id", {"message_id": "not-the-same-id"}),
        ("timestamp changed", {"timestamp": TIMESTAMP + 1}),
    ],
)
def test_any_signed_field_changing_invalidates_it(label: str, kwargs: dict[str, object]) -> None:
    sig = signing.sign(SECRET, MESSAGE_ID, TIMESTAMP, BODY)
    args = {
        "secret": SECRET,
        "message_id": MESSAGE_ID,
        "timestamp": TIMESTAMP,
        "body": BODY,
        "header_value": sig,
        "now": TIMESTAMP,
    }
    args.update(kwargs)
    assert signing.verify(**args) is False, label  # type: ignore[arg-type]


def test_a_single_flipped_byte_in_the_body_fails() -> None:
    sig = signing.sign(SECRET, MESSAGE_ID, TIMESTAMP, BODY)
    tampered = bytearray(BODY)
    tampered[10] ^= 0x01
    assert not signing.verify(SECRET, MESSAGE_ID, TIMESTAMP, bytes(tampered), sig, now=TIMESTAMP)


def test_timestamp_outside_tolerance_is_rejected() -> None:
    """Replay protection.

    Signing only the body would leave a captured request valid for ever - anyone who
    records one could replay it indefinitely. Including the timestamp is what makes a stale
    request detectable.
    """
    sig = signing.sign(SECRET, MESSAGE_ID, TIMESTAMP, BODY)
    assert signing.verify(SECRET, MESSAGE_ID, TIMESTAMP, BODY, sig, now=TIMESTAMP + 299)
    assert not signing.verify(SECRET, MESSAGE_ID, TIMESTAMP, BODY, sig, now=TIMESTAMP + 301)


def test_a_future_timestamp_is_also_rejected() -> None:
    """The window is symmetric, so a clock far ahead of ours is caught too."""
    sig = signing.sign(SECRET, MESSAGE_ID, TIMESTAMP, BODY)
    assert not signing.verify(SECRET, MESSAGE_ID, TIMESTAMP, BODY, sig, now=TIMESTAMP - 301)


def test_tolerance_is_configurable() -> None:
    sig = signing.sign(SECRET, MESSAGE_ID, TIMESTAMP, BODY)
    assert signing.verify(
        SECRET, MESSAGE_ID, TIMESTAMP, BODY, sig, now=TIMESTAMP + 1000, tolerance_seconds=2000
    )


def test_rotation_accepts_either_signature() -> None:
    """Space-separated list, so a rotation window has no downtime.

    During a change a request can carry both the old and the new signature and a receiver
    accepting either sees nothing.
    """
    old = signing.sign("whsec_old", MESSAGE_ID, TIMESTAMP, BODY)
    new = signing.sign(SECRET, MESSAGE_ID, TIMESTAMP, BODY)
    header = f"{old} {new}"

    assert signing.verify(SECRET, MESSAGE_ID, TIMESTAMP, BODY, header, now=TIMESTAMP)
    assert signing.verify("whsec_old", MESSAGE_ID, TIMESTAMP, BODY, header, now=TIMESTAMP)
    assert not signing.verify("whsec_third", MESSAGE_ID, TIMESTAMP, BODY, header, now=TIMESTAMP)


def test_garbage_header_does_not_raise() -> None:
    """A malformed signature is a failed verification, not a 500."""
    assert not signing.verify(SECRET, MESSAGE_ID, TIMESTAMP, BODY, "", now=TIMESTAMP)
    assert not signing.verify(SECRET, MESSAGE_ID, TIMESTAMP, BODY, "nonsense", now=TIMESTAMP)
    assert not signing.verify(SECRET, MESSAGE_ID, TIMESTAMP, BODY, "v9,zzz", now=TIMESTAMP)


def test_empty_body_is_signable() -> None:
    sig = signing.sign(SECRET, MESSAGE_ID, TIMESTAMP, b"")
    assert signing.verify(SECRET, MESSAGE_ID, TIMESTAMP, b"", sig, now=TIMESTAMP)


def test_build_headers_carries_everything_a_receiver_needs() -> None:
    headers = signing.build_headers(
        secret=SECRET,
        message_id=MESSAGE_ID,
        timestamp=TIMESTAMP,
        body=BODY,
        user_agent="hookline/0.1.0",
    )
    assert headers["webhook-id"] == MESSAGE_ID
    assert headers["webhook-timestamp"] == str(TIMESTAMP)
    assert headers["content-type"] == "application/json"
    assert headers["user-agent"] == "hookline/0.1.0"
    assert signing.verify(
        SECRET, MESSAGE_ID, TIMESTAMP, BODY, headers["webhook-signature"], now=TIMESTAMP
    )
