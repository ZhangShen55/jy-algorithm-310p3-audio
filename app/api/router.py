from __future__ import annotations

from fastapi import APIRouter

from app.api.routes.asr import router as asr_router
from app.api.routes.health import router as health_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(asr_router)
