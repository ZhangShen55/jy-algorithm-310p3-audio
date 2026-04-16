from __future__ import annotations

import logging
from logging.config import dictConfig
from pathlib import Path

from app.core.config import LoggingSettings


def setup_logging(config: LoggingSettings) -> None:
    log_dir = Path(config.log_dir).expanduser().resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / config.file_name

    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {
                    "format": "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "standard",
                    "level": config.level,
                },
                "file": {
                    "class": "logging.handlers.RotatingFileHandler",
                    "formatter": "standard",
                    "level": config.level,
                    "filename": str(log_file),
                    "maxBytes": config.max_bytes,
                    "backupCount": config.backup_count,
                    "encoding": "utf-8",
                },
            },
            "root": {
                "handlers": ["console", "file"],
                "level": config.level,
            },
            "loggers": {
                "uvicorn.error": {
                    "handlers": ["console", "file"],
                    "level": config.level,
                    "propagate": False,
                },
                "uvicorn.access": {
                    "handlers": ["console", "file"],
                    "level": config.level,
                    "propagate": False,
                },
            },
        }
    )

    logging.getLogger(__name__).info("Logger initialized. file=%s", log_file)
