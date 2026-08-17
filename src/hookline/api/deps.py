from typing import Annotated

from fastapi import Depends

from hookline.config import Settings, get_settings
from hookline.store import EndpointStore, get_endpoint_store

SettingsDep = Annotated[Settings, Depends(get_settings)]
StoreDep = Annotated[EndpointStore, Depends(get_endpoint_store)]