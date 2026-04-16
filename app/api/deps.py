from __future__ import annotations

from functools import lru_cache

from app.core.config import get_settings
from app.services.asr_service import ASRService


@lru_cache(maxsize=1)
def get_asr_service() -> ASRService:
    settings = get_settings()
    return ASRService(settings=settings)
