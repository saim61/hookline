from fastapi import APIRouter

from hookline.api.v1.routes import endpoints

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(endpoints.router)
