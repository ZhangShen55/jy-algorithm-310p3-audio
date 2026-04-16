from __future__ import annotations

import logging
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.core.config import get_settings
from app.core.logging import setup_logging


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.logging)

    app = FastAPI(
        title=settings.app.name,
        version=settings.app.version,
    )
    app.state.settings = settings
    app.include_router(api_router)

    request_logger = logging.getLogger("app.request")

    @app.middleware("http")
    async def request_log_middleware(request: Request, call_next):
        request_id = uuid4().hex
        request.state.request_id = request_id
        start_time = perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (perf_counter() - start_time) * 1000
            request_logger.exception(
                "request.failed request_id=%s method=%s path=%s elapsed_ms=%.2f",
                request_id,
                request.method,
                request.url.path,
                elapsed_ms,
            )
            raise

        elapsed_ms = (perf_counter() - start_time) * 1000
        response.headers["X-Request-ID"] = request_id
        request_logger.info(
            "request.completed request_id=%s method=%s path=%s status=%s elapsed_ms=%.2f",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response

    @app.on_event("startup")
    async def startup_event() -> None:
        Path(settings.storage.tmp_dir).expanduser().resolve().mkdir(parents=True, exist_ok=True)
        logging.getLogger(__name__).info("Application started. env=%s", settings.app.env)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logging.getLogger(__name__).exception(
            "Unhandled exception. path=%s request_id=%s",
            request.url.path,
            getattr(request.state, "request_id", "-"),
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error."},
        )

    return app


app = create_app()
