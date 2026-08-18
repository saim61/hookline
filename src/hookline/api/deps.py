from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from hookline.config import Settings, get_settings
from hookline.db.session import get_session
from hookline.repositories.endpoint import EndpointRepository

SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_endpoint_repo(session: SessionDep) -> EndpointRepository:
    return EndpointRepository(session)


RepoDep = Annotated[EndpointRepository, Depends(get_endpoint_repo)]
