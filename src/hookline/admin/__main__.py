"""Key management from the command line: `uv run hookline-admin`.

This exists to break a chicken-and-egg problem. Creating an API key requires the `admin`
scope, which requires an API key, so the very first key cannot be created over HTTP. The
CLI talks to Postgres directly - anyone who can run it already has database credentials,
so it grants nothing they did not already have.

Also useful for revoking a leaked key when the API itself is what is misbehaving.
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta

from hookline.auth.scopes import Scope, is_valid
from hookline.db.session import dispose_engine, get_sessionmaker
from hookline.repositories.api_key import ApiKeyRepository


async def _create(name: str, scope_list: list[str], days: int | None, signed: bool) -> int:
    unknown = sorted({s for s in scope_list if not is_valid(s)})
    if unknown:
        print(f"error: unknown scopes: {', '.join(unknown)}", file=sys.stderr)
        print(f"known:  {', '.join(sorted(s.value for s in Scope))}", file=sys.stderr)
        return 2

    expires_at = datetime.now(UTC) + timedelta(days=days) if days else None

    async with get_sessionmaker()() as session:
        repo = ApiKeyRepository(session)
        api_key, token = await repo.create(
            name=name,
            scopes=scope_list,
            expires_at=expires_at,
            with_inbound_signing=signed,
        )
        await session.commit()

    print()
    print(f"  id       {api_key.id}")
    print(f"  name     {api_key.name}")
    print(f"  scopes   {', '.join(api_key.scopes)}")
    print(f"  expires  {expires_at.isoformat() if expires_at else 'never'}")
    print()
    print("  key      " + token)
    if api_key.inbound_signing_secret:
        print("  signing  " + api_key.inbound_signing_secret)
        print()
        print("  This key requires signed requests. Sign the body with the signing secret")
        print("  and send webhook-id, webhook-timestamp and webhook-signature.")
    print()
    print("  Only a hash is stored, so this is the one and only time the key is shown.")
    print()
    return 0


async def _list() -> int:
    async with get_sessionmaker()() as session:
        rows = await ApiKeyRepository(session).list_all()

    if not rows:
        print("no api keys. create one with: uv run hookline-admin create-key --name ...")
        return 0

    print(f"{'id':38} {'prefix':14} {'name':22} {'state':9} {'last used':22} scopes")
    for k in rows:
        state = "active" if k.is_usable(datetime.now(UTC)) else "unusable"
        last = k.last_used_at.isoformat(timespec="seconds") if k.last_used_at else "never"
        print(
            f"{k.id!s:38} {k.display_prefix:14} {k.name[:22]:22} "
            f"{state:9} {last:22} {','.join(k.scopes)}"
        )
    return 0


async def _revoke(key_id: str) -> int:
    from uuid import UUID

    try:
        parsed = UUID(key_id)
    except ValueError:
        print(f"error: {key_id!r} is not a uuid", file=sys.stderr)
        return 2

    async with get_sessionmaker()() as session:
        repo = ApiKeyRepository(session)
        if await repo.revoke(parsed):
            await session.commit()
            print(f"revoked {parsed}")
            return 0
        existing = await repo.get(parsed)
    print(
        f"error: {'already revoked' if existing else 'no such key'}: {parsed}",
        file=sys.stderr,
    )
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hookline-admin", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-key", help="mint an api key")
    create.add_argument("--name", required=True, help="what this key is for")
    create.add_argument(
        "--scopes",
        default=Scope.ADMIN.value,
        help="comma separated. defaults to admin, which is what a bootstrap key needs",
    )
    create.add_argument("--expires-days", type=int, default=None, help="omit for no expiry")
    create.add_argument(
        "--signed-requests",
        action="store_true",
        help="also require a valid HMAC signature on requests using this key",
    )

    sub.add_parser("list-keys", help="list api keys, without their secrets")

    revoke = sub.add_parser("revoke-key", help="deactivate a key, keeping it for audit")
    revoke.add_argument("key_id")

    return parser


async def _dispatch(args: argparse.Namespace) -> int:
    try:
        match args.command:
            case "create-key":
                return await _create(
                    args.name,
                    [s.strip() for s in args.scopes.split(",") if s.strip()],
                    args.expires_days,
                    args.signed_requests,
                )
            case "list-keys":
                return await _list()
            case "revoke-key":
                return await _revoke(args.key_id)
        return 2
    finally:
        await dispose_engine()


def main() -> None:
    args = build_parser().parse_args()
    try:
        sys.exit(asyncio.run(_dispatch(args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
