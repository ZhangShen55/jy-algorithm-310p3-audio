from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class AudioMetricItem(BaseModel):
    bg: float
    ed: float
    max_db: float
    db: float
    min_db: float
    snr: float
    max_volume: int
    volume: int
    min_volume: int

    model_config = ConfigDict(extra="forbid")


class AudioMetricsResponse(BaseModel):
    result: list[AudioMetricItem]
    task_id: str
    process_time_ms: int
    timestamp: int

    model_config = ConfigDict(extra="forbid")
