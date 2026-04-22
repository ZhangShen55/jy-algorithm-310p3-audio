from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from app.api.deps import get_asr_service
from app.core.errors import ASRProcessingError, ConcurrencyLimitError
from app.schemas.asr import AsrResponse
from app.services.asr_service import ASRService

router = APIRouter(tags=["asr"])
logger = logging.getLogger(__name__)


@router.post(
    "/v1.1.8/seacraft_asr",
    response_model=AsrResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_200_OK,
)
async def seacraft_asr(
    audioFile: UploadFile = File(...),
    showSpk: bool = Form(default=False),
    showEmotion: bool = Form(default=False),
    language: str | None = Form(default=None),
    openPunc: bool = Form(default=True),
    asr_service: ASRService = Depends(get_asr_service),
) -> AsrResponse:
    if showSpk:
        logger.warning("showSpk=true was requested but speaker diarization is not implemented yet.")

    if not audioFile.filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="audioFile filename is empty.")

    try:
        return await asr_service.transcribe(
            upload_file=audioFile,
            show_emotion=showEmotion,
            language=language,
            open_punc=openPunc,
        )
    except ConcurrencyLimitError as exc:
        logger.warning("Concurrency limit exceeded. max_concurrent=%s", exc.details)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=exc.message,
        ) from exc
    except ASRProcessingError as exc:
        logger.exception("ASR processing failed. details=%s", exc.details)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=exc.message,
        ) from exc


@router.get("/get_status", status_code=status.HTTP_200_OK)
async def get_status(asr_service: ASRService = Depends(get_asr_service)) -> dict:
    """获取 ASR 服务状态，包括成功/失败数量、正在处理和排队的任务"""
    return await asr_service.get_status()
