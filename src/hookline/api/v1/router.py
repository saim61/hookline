from fastapi import APIRouter

from hookline.api.v1.routes import deliveries, endpoints, events

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(endpoints.router)
api_router.include_router(events.router)
api_router.include_router(deliveries.router)
