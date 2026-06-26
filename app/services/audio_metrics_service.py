from __future__ import annotations

import asyncio
import logging
import math
import sys
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter, time
from uuid import uuid4

from fastapi import UploadFile

from app.core.config import AudioMetricsSettings, Settings
from app.core.errors import AudioMetricsProcessingError
from app.schemas.audio_metrics import AudioMetricItem, AudioMetricsResponse


@dataclass(frozen=True)
class AudioMetricsAnalyzerConfig:
    analysis_frame_ms: int = 100
    db_floor: float = -60.0


class AudioMetricsAnalyzer:
    _PCM_16_FULL_SCALE = 32768.0

    def __init__(self, config: AudioMetricsAnalyzerConfig | None = None) -> None:
        self._config = config or AudioMetricsAnalyzerConfig()

    def analyze_wave(self, audio_path: Path, *, time_size: int) -> list[AudioMetricItem]:
        if time_size <= 0:
            raise ValueError("time_size must be greater than 0")

        sample_rate, samples = self._read_pcm_wave(audio_path)
        if not samples:
            raise AudioMetricsProcessingError("Decoded audio contains no samples.")

        window_sample_count = max(1, int(sample_rate * time_size))
        result: list[AudioMetricItem] = []

        for start_sample in range(0, len(samples), window_sample_count):
            end_sample = min(start_sample + window_sample_count, len(samples))
            window_samples = samples[start_sample:end_sample]
            result.append(
                self._build_metric_item(
                    samples=window_samples,
                    sample_rate=sample_rate,
                    start_sample=start_sample,
                    end_sample=end_sample,
                )
            )

        return result

    def db_to_volume(self, db_value: float) -> int:
        db_floor = self._config.db_floor
        if db_value <= db_floor:
            return 0
        if db_value >= 0:
            return 100

        ratio = (db_value - db_floor) / (0 - db_floor)
        return int(round(max(0.0, min(1.0, ratio)) * 100))

    def _build_metric_item(
        self,
        *,
        samples: list[int],
        sample_rate: int,
        start_sample: int,
        end_sample: int,
    ) -> AudioMetricItem:
        frame_powers = self._frame_powers(samples, sample_rate)
        frame_dbs = [self._power_to_db(power) for power in frame_powers]
        window_db = self._power_to_db(self._mean_square(samples))

        max_db = max(frame_dbs) if frame_dbs else window_db
        min_db = min(frame_dbs) if frame_dbs else window_db

        return AudioMetricItem(
            bg=round(start_sample / sample_rate, 2),
            ed=round(end_sample / sample_rate, 2),
            max_db=max_db,
            db=window_db,
            min_db=min_db,
            snr=self._estimate_snr(frame_powers),
            max_volume=self.db_to_volume(max_db),
            volume=self.db_to_volume(window_db),
            min_volume=self.db_to_volume(min_db),
        )

    def _frame_powers(self, samples: list[int], sample_rate: int) -> list[float]:
        frame_size = max(1, int(round(sample_rate * self._config.analysis_frame_ms / 1000)))
        return [
            self._mean_square(samples[offset : offset + frame_size])
            for offset in range(0, len(samples), frame_size)
        ]

    @staticmethod
    def _mean_square(samples: list[int]) -> float:
        if not samples:
            return 0.0
        return sum(sample * sample for sample in samples) / len(samples)

    def _power_to_db(self, power: float) -> float:
        if power <= 0:
            return round(self._config.db_floor, 2)

        rms = math.sqrt(power)
        db_value = 20 * math.log10(rms / self._PCM_16_FULL_SCALE)
        floored = max(self._config.db_floor, min(0.0, db_value))
        return round(floored, 2)

    @staticmethod
    def _estimate_snr(frame_powers: list[float]) -> float:
        positive_powers = sorted(power for power in frame_powers if power > 0)
        if not positive_powers:
            return 0.0

        bucket_size = max(1, math.ceil(len(positive_powers) * 0.1))
        noise_power = sum(positive_powers[:bucket_size]) / bucket_size
        signal_power = sum(positive_powers[-bucket_size:]) / bucket_size

        if signal_power <= noise_power * 1.000001:
            return 0.0

        noise_power = max(noise_power, 1.0)
        return round(max(0.0, 10 * math.log10(signal_power / noise_power)), 2)

    @staticmethod
    def _read_pcm_wave(audio_path: Path) -> tuple[int, list[int]]:
        try:
            with wave.open(str(audio_path), "rb") as handle:
                channels = handle.getnchannels()
                sample_width = handle.getsampwidth()
                sample_rate = handle.getframerate()
                frame_count = handle.getnframes()
                raw_frames = handle.readframes(frame_count)
        except wave.Error as exc:
            raise AudioMetricsProcessingError(f"Invalid decoded WAV file: {exc}") from exc

        if sample_width != 2:
            raise AudioMetricsProcessingError("Decoded WAV must be signed 16-bit PCM.")
        if channels <= 0:
            raise AudioMetricsProcessingError("Decoded WAV has no audio channels.")

        pcm_samples = array("h")
        pcm_samples.frombytes(raw_frames)
        if sys.byteorder != "little":
            pcm_samples.byteswap()

        if channels == 1:
            return sample_rate, list(pcm_samples)

        mono_samples = [
            int(round(sum(pcm_samples[index : index + channels]) / channels))
            for index in range(0, len(pcm_samples), channels)
        ]
        return sample_rate, mono_samples


class AudioMetricsService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._config = settings.audio_metrics
        self._logger = logging.getLogger(self.__class__.__name__)
        self._tmp_dir = Path(settings.storage.tmp_dir).expanduser().resolve()
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        self._analyzer = AudioMetricsAnalyzer(
            AudioMetricsAnalyzerConfig(
                analysis_frame_ms=self._config.analysis_frame_ms,
                db_floor=self._config.db_floor,
            )
        )

    async def analyze(self, *, upload_file: UploadFile, time_size: int) -> AudioMetricsResponse:
        if time_size <= 0:
            raise ValueError("time_size must be greater than 0")

        request_start = perf_counter()
        source_path = self._build_temp_path(upload_file.filename)
        decoded_path = source_path.with_suffix(".decoded.wav")
        task_id = f"task-{uuid4().hex[:8]}_{self._safe_filename(upload_file.filename)}"

        try:
            await self._save_upload_file(upload_file, source_path)
            await self._decode_to_wave(source_path, decoded_path)
            metrics = self._analyzer.analyze_wave(decoded_path, time_size=time_size)

            return AudioMetricsResponse(
                result=metrics,
                task_id=task_id,
                process_time_ms=int(round((perf_counter() - request_start) * 1000)),
                timestamp=int(time()),
            )
        finally:
            await self._delete_temp_file(source_path)
            await self._delete_temp_file(decoded_path)

    async def _save_upload_file(self, upload_file: UploadFile, target_path: Path) -> None:
        try:
            with target_path.open("wb") as destination:
                while True:
                    chunk = await upload_file.read(self._settings.storage.chunk_size)
                    if not chunk:
                        break
                    destination.write(chunk)
        except OSError as exc:
            raise AudioMetricsProcessingError("Failed to persist uploaded audio file.") from exc
        finally:
            await upload_file.close()

    async def _decode_to_wave(self, source_path: Path, target_path: Path) -> None:
        config = self._config
        command = self._build_ffmpeg_command(source_path, target_path, config)

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise AudioMetricsProcessingError(
                f"FFmpeg executable not found: {config.ffmpeg_executable}"
            ) from exc

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=config.command_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise AudioMetricsProcessingError("FFmpeg decode command timed out.") from exc

        stdout_text = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        stderr_text = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""

        if process.returncode != 0:
            details = self._tail_text(stderr_text) or self._tail_text(stdout_text)
            raise AudioMetricsProcessingError("FFmpeg failed to decode uploaded audio.", details=details)

        if stderr_text.strip():
            self._logger.debug("FFmpeg stderr: %s", self._tail_text(stderr_text))

        if not target_path.exists() or target_path.stat().st_size == 0:
            raise AudioMetricsProcessingError("FFmpeg decode produced an empty WAV file.")

    @staticmethod
    def _build_ffmpeg_command(source_path: Path, target_path: Path, config: AudioMetricsSettings) -> list[str]:
        return [
            config.ffmpeg_executable,
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            str(source_path),
            "-ac",
            "1",
            "-ar",
            str(config.decode_sample_rate),
            "-sample_fmt",
            "s16",
            str(target_path),
        ]

    def _build_temp_path(self, filename: str | None) -> Path:
        safe_name = self._safe_filename(filename)
        suffix = Path(safe_name).suffix or ".audio"
        return self._tmp_dir / f"{uuid4().hex}{suffix}"

    @staticmethod
    def _safe_filename(filename: str | None) -> str:
        raw_name = filename or "audio"
        return Path(raw_name).name.replace("/", "_").replace("\\", "_") or "audio"

    async def _delete_temp_file(self, target_path: Path) -> None:
        if not target_path.exists():
            return

        try:
            target_path.unlink(missing_ok=True)
        except OSError as exc:
            self._logger.warning("Failed to remove temporary audio file %s: %s", target_path, exc)

    @staticmethod
    def _tail_text(text: str, *, lines: int = 20) -> str:
        split_lines = text.strip().splitlines()
        if not split_lines:
            return ""
        return "\n".join(split_lines[-lines:])
