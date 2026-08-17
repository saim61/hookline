from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class EndpointCreate(BaseModel):
    url: HttpUrl
    description: str | None = Field(default=None, max_length=200)
    event_types: list[str] = Field(default_factory=list)

    @field_validator("event_types")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("event_types must be unique")
        return v


class EndpointRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    url: HttpUrl
    description: str | None
    event_types: list[str]
    is_active: bool
    created_at: datetime


class EndpointCreated(EndpointRead):
    signing_secret: str