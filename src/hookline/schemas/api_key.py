from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from hookline.auth.scopes import Scope, is_valid


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(min_length=1)
    expires_at: datetime | None = None
    # When true the key gets a signing secret and requests authenticated with it must
    # carry a valid signature over the body.
    require_signed_requests: bool = False

    @field_validator("scopes")
    @classmethod
    def _known_scopes(cls, v: list[str]) -> list[str]:
        unknown = sorted({s for s in v if not is_valid(s)})
        if unknown:
            known = ", ".join(sorted(s.value for s in Scope))
            raise ValueError(f"unknown scopes: {', '.join(unknown)}. known scopes: {known}")
        if len(set(v)) != len(v):
            raise ValueError("scopes must be unique")
        return v


class ApiKeyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    display_prefix: str
    scopes: list[str]
    is_active: bool
    signed_requests_required: bool
    expires_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime


class ApiKeyCreated(ApiKeyRead):
    """The only response that ever contains the credential itself.

    Same pattern as EndpointCreated: the secret lives in a separate model, so a route
    returning `response_model=list[ApiKeyRead]` cannot leak it even if the handler hands
    over the full row.
    """

    key: str
    inbound_signing_secret: str | None = None
