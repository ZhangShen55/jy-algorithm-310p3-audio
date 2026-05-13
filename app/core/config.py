from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


class AppSettings(BaseModel):
    name: str = "Seacraft ASR Service"
    version: str = "1.1.8"
    env: str = "prod"


class ServerSettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    reload: bool = False
    workers: int = 1


class LoggingSettings(BaseModel):
    level: str = "INFO"
    log_dir: str = "./logs"
    file_name: str = "app.log"
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 10


class StorageSettings(BaseModel):
    tmp_dir: str = "./tmp_audio"
    chunk_size: int = 1024 * 1024


class ParaformerCliSettings(BaseModel):
    executable: str
    working_dir: str | None = None
    provider: str = "ascend"
    num_threads: int = 2
    silero_vad_model: str
    silero_vad_threshold: float = 0.35
    silero_vad_min_silence_duration: float = 0.25
    paraformer: str
    tokens: str
    command_timeout_seconds: int = 7200


class SenseVoiceCliSettings(BaseModel):
    executable: str
    working_dir: str | None = None
    provider: str = "ascend"
    num_threads: int = 2
    silero_vad_model: str
    silero_vad_threshold: float = 0.4
    silero_vad_min_silence_duration: float = 0.25
    sense_voice_model: str
    tokens: str
    command_timeout_seconds: int = 7200


class PunctuationCliSettings(BaseModel):
    executable: str
    working_dir: str | None = None
    ct_transformer: str


class AsrPipelineSettings(BaseModel):
    paraformer: ParaformerCliSettings
    sensevoice: SenseVoiceCliSettings
    max_concurrent_requests: int = 16
    max_queue_size: int = 10


class Settings(BaseModel):
    app: AppSettings = Field(default_factory=AppSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    asr: AsrPipelineSettings
    punctuation_cli: PunctuationCliSettings


def _load_toml_file(config_path: Path) -> dict:
    with config_path.open("rb") as handle:
        return tomllib.load(handle)


def resolved_config_path(config_file: str | None = None) -> Path:
    """与 get_settings 使用相同的规则定位 config.toml（绝对路径）。"""
    raw_path = config_file or os.environ.get("APP_CONFIG_FILE", "config.toml")
    return Path(raw_path).expanduser().resolve()


def resolved_config_dir(config_file: str | None = None) -> Path:
    """config.toml 所在目录；CLI 的相对 working_dir 以此为基准解析。"""
    return resolved_config_path(config_file).parent


@lru_cache(maxsize=1)
def get_settings(config_file: str | None = None) -> Settings:
    config_path = resolved_config_path(config_file)

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    config_data = _load_toml_file(config_path)

    try:
        return Settings.model_validate(config_data)
    except ValidationError as exc:
        raise ValueError(f"Invalid config file: {config_path}") from exc
