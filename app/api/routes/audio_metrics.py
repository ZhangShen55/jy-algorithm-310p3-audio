from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from app.api.deps import get_audio_metrics_service
from app.core.errors import AudioMetricsProcessingError
from app.schemas.audio_metrics import AudioMetricsResponse
from app.services.audio_metrics_service import AudioMetricsService

router = APIRouter(tags=["audio_metrics"])
logger = logging.getLogger(__name__)


@router.post(
    "/audio/db_snr",
    response_model=AudioMetricsResponse,
    status_code=status.HTTP_200_OK,
)
async def audio_db_snr(
    audioFile: UploadFile | str | None = File(...),
    time_size: int = Form(...),
    audio_metrics_service: AudioMetricsService = Depends(get_audio_metrics_service),
) -> AudioMetricsResponse:
    if isinstance(audioFile, str) or audioFile is None or not getattr(audioFile, "filename", None):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="audioFile filename is empty.")

    if time_size <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="time_size must be greater than 0.")

    try:
        return await audio_metrics_service.analyze(upload_file=audioFile, time_size=time_size)
    except AudioMetricsProcessingError as exc:
        logger.exception("Audio metrics processing failed. details=%s", exc.details)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=exc.message,
        ) from exc
