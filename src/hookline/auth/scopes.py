"""Scopes: what a key is allowed to do.

Separate read and write per resource so a key can be given exactly what it needs. In
practice the SaaS company's checkout service only ever ingests events, so it gets
`events:write` and nothing else - and a leak of that key cannot read customers' signing
secrets, delete endpoints, or mint new keys.
"""

from enum import StrEnum


class Scope(StrEnum):
    ENDPOINTS_READ = "endpoints:read"
    ENDPOINTS_WRITE = "endpoints:write"
    EVENTS_READ = "events:read"
    EVENTS_WRITE = "events:write"
    DELIVERIES_READ = "deliveries:read"
    DELIVERIES_WRITE = "deliveries:write"
    # Everything, including managing other keys. Only the bootstrap key should hold this.
    ADMIN = "admin"


ALL_SCOPES = frozenset(Scope)


def is_valid(scope: str) -> bool:
    return scope in {s.value for s in Scope}


def grants(held: frozenset[str], required: Scope) -> bool:
    """Whether `held` satisfies `required`.

    `admin` is a wildcard. Making it implicit rather than expanding it at creation time
    means a key created today automatically covers a scope added next month - which is
    what an operator expects from something called admin, and avoids silently
    under-privileged keys after every release.
    """
    return Scope.ADMIN in held or required in held
