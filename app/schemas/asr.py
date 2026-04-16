from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Segment(BaseModel):
    segment_text: str
    bg: str
    ed: str
    speed: int
    role: str | None = None
    emotion: str | None = None

    model_config = ConfigDict(extra="forbid")


class AsrResponse(BaseModel):
    language: str
    segments: list[Segment]
    text: str
    load_audio_time_ms: str
    gpu_time_ms: str

    model_config = ConfigDict(extra="forbid")
