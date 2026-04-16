from __future__ import annotations

import asyncio
import json
import logging
import shlex
import wave
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from fastapi import UploadFile

from app.core.config import ParaformerCliSettings, SenseVoiceCliSettings, Settings
from app.core.errors import ASRProcessingError
from app.schemas.asr import AsrResponse, Segment


@dataclass(frozen=True)
class TimelineSegment:
    start: float
    end: float
    text: str


class ASRService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._logger = logging.getLogger(self.__class__.__name__)
        self._tmp_dir = Path(settings.storage.tmp_dir).expanduser().resolve()
        self._tmp_dir.mkdir(parents=True, exist_ok=True)

    async def transcribe(
        self,
        *,
        upload_file: UploadFile,
        show_emotion: bool,
        language: str | None,
    ) -> AsrResponse:
        temp_audio_path = self._build_temp_path(upload_file.filename)
        normalized_language = self._normalize_language(language)

        try:
            load_start = perf_counter()
            await self._save_upload_file(upload_file, temp_audio_path)
            load_audio_time_ms = (perf_counter() - load_start) * 1000

            self._logger.info(
                "Audio upload saved. source_name=%s temp_path=%s size_bytes=%s",
                upload_file.filename,
                temp_audio_path,
                temp_audio_path.stat().st_size if temp_audio_path.exists() else -1,
            )

            gpu_start = perf_counter()
            timeline_segments = await self._run_paraformer(temp_audio_path)
            segment_emotions = await self._extract_emotions(
                audio_path=temp_audio_path,
                timeline_segments=timeline_segments,
                show_emotion=show_emotion,
                language=normalized_language,
            )
            gpu_time_ms = (perf_counter() - gpu_start) * 1000

            response_segments: list[Segment] = []
            merged_text_parts: list[str] = []
            for idx, timeline in enumerate(timeline_segments):
                duration = max(0.0, timeline.end - timeline.start)
                response_segments.append(
                    Segment(
                        segment_text=timeline.text,
                        bg=f"{timeline.start:.2f}",
                        ed=f"{timeline.end:.2f}",
                        speed=self._calc_speed(timeline.text, duration),
                        role=None,
                        emotion=segment_emotions[idx],
                    )
                )
                merged_text_parts.append(timeline.text)

            return AsrResponse(
                language=normalized_language or "auto",
                segments=response_segments,
                text="".join(merged_text_parts),
                load_audio_time_ms=f"{load_audio_time_ms:.2f}",
                gpu_time_ms=f"{gpu_time_ms:.2f}",
            )
        finally:
            await self._delete_temp_file(temp_audio_path)

    async def _run_paraformer(self, audio_path: Path) -> list[TimelineSegment]:
        paraformer_config = self._settings.asr.paraformer
        command = self._build_paraformer_command(audio_path, paraformer_config)
        stdout_text = await self._run_command(
            command=command,
            timeout_seconds=paraformer_config.command_timeout_seconds,
            working_dir=paraformer_config.working_dir,
            command_label="paraformer",
        )
        return self._parse_paraformer_segments(stdout_text)

    async def _extract_emotions(
        self,
        *,
        audio_path: Path,
        timeline_segments: list[TimelineSegment],
        show_emotion: bool,
        language: str | None,
    ) -> list[str | None]:
        if not timeline_segments:
            return []

        if not show_emotion:
            return [None] * len(timeline_segments)

        split_dir, chunk_paths = self._split_audio_by_segments(audio_path, timeline_segments)
        emotions: list[str | None] = []

        try:
            for index, chunk_path in enumerate(chunk_paths):
                emotion = await self._run_sensevoice_for_chunk(chunk_path, language, index)
                emotions.append(emotion)
        finally:
            for chunk_path in chunk_paths:
                await self._delete_temp_file(chunk_path)
            await self._delete_temp_dir(split_dir)

        return emotions

    async def _run_sensevoice_for_chunk(self, chunk_path: Path, language: str | None, index: int) -> str | None:
        sensevoice_config = self._settings.asr.sensevoice
        command = self._build_sensevoice_command(chunk_path, language, sensevoice_config)

        try:
            stdout_text = await self._run_command(
                command=command,
                timeout_seconds=sensevoice_config.command_timeout_seconds,
                working_dir=sensevoice_config.working_dir,
                command_label=f"sensevoice[{index}]",
            )
        except ASRProcessingError as exc:
            self._logger.warning(
                "SenseVoice emotion extraction failed for chunk index=%s path=%s details=%s",
                index,
                chunk_path,
                exc.details,
            )
            return None

        return self._parse_emotion(stdout_text)

    def _split_audio_by_segments(
        self,
        audio_path: Path,
        timeline_segments: list[TimelineSegment],
    ) -> tuple[Path, list[Path]]:
        split_dir = self._tmp_dir / f"split_{uuid4().hex}"
        split_dir.mkdir(parents=True, exist_ok=True)
        chunk_paths: list[Path] = []

        try:
            with wave.open(str(audio_path), "rb") as source:
                total_frames = source.getnframes()
                frame_rate = source.getframerate()
                channels = source.getnchannels()
                sample_width = source.getsampwidth()

                if frame_rate <= 0:
                    raise ASRProcessingError("Invalid audio frame rate.")

                for index, seg in enumerate(timeline_segments):
                    start_frame = int(max(0.0, seg.start) * frame_rate)
                    end_frame = int(max(seg.start, seg.end) * frame_rate)

                    if total_frames > 0:
                        start_frame = min(start_frame, total_frames - 1)
                        end_frame = min(end_frame, total_frames)
                        if end_frame <= start_frame:
                            end_frame = min(total_frames, start_frame + 1)
                    else:
                        start_frame = 0
                        end_frame = 0

                    frame_count = max(0, end_frame - start_frame)
                    source.setpos(start_frame)
                    frames = source.readframes(frame_count)

                    chunk_path = split_dir / f"chunk_{index:06d}.wav"
                    with wave.open(str(chunk_path), "wb") as chunk:
                        chunk.setnchannels(channels)
                        chunk.setsampwidth(sample_width)
                        chunk.setframerate(frame_rate)
                        chunk.writeframes(frames)

                    chunk_paths.append(chunk_path)
        except wave.Error as exc:
            raise ASRProcessingError(
                "Failed to split audio by paraformer timestamps. Ensure uploaded audio is PCM WAV.",
                details=str(exc),
            ) from exc

        self._logger.info("Audio split finished. chunk_count=%s split_dir=%s", len(chunk_paths), split_dir)
        return split_dir, chunk_paths

    async def _run_command(
        self,
        *,
        command: list[str],
        timeout_seconds: int,
        working_dir: str | None,
        command_label: str,
    ) -> str:
        cwd = self._resolve_working_dir(working_dir)
        self._logger.info(
            "Executing %s command: cwd=%s cmd=%s",
            command_label,
            cwd or "<inherit>",
            " ".join(shlex.quote(part) for part in command),
        )

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ASRProcessingError(f"{command_label} command timed out.") from exc

        stdout_text = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        stderr_text = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""

        if process.returncode != 0:
            stderr_tail = self._tail_text(stderr_text)
            stdout_tail = self._tail_text(stdout_text)
            self._logger.error(
                "%s command failed. return_code=%s stderr_tail=%s stdout_tail=%s",
                command_label,
                process.returncode,
                stderr_tail,
                stdout_tail,
            )
            raise ASRProcessingError(
                f"{command_label} command failed with exit code {process.returncode}.",
                details=stderr_tail or stdout_tail,
            )

        if stderr_text.strip():
            self._logger.warning("%s command stderr: %s", command_label, self._tail_text(stderr_text))

        return stdout_text

    def _build_paraformer_command(self, audio_path: Path, config: ParaformerCliSettings) -> list[str]:
        command = [
            config.executable,
            f"--provider={config.provider}",
            f"--silero-vad-model={config.silero_vad_model}",
            f"--silero-vad-threshold={config.silero_vad_threshold}",
            f"--silero-vad-min-silence-duration={config.silero_vad_min_silence_duration}",
            f"--paraformer={config.paraformer}",
            f"--tokens={config.tokens}",
            str(audio_path),
        ]
        return command

    def _build_sensevoice_command(
        self,
        audio_path: Path,
        language: str | None,
        config: SenseVoiceCliSettings,
    ) -> list[str]:
        command = [
            config.executable,
            f"--provider={config.provider}",
            f"--silero-vad-model={config.silero_vad_model}",
            f"--silero-vad-threshold={config.silero_vad_threshold}",
            f"--silero-vad-min-silence-duration={config.silero_vad_min_silence_duration}",
            f"--sense-voice-model={config.sense_voice_model}",
            f"--tokens={config.tokens}",
        ]

        if language:
            command.append(f"--sense-voice-language={language}")

        command.append(str(audio_path))
        return command

    def _parse_paraformer_segments(self, stdout_text: str) -> list[TimelineSegment]:
        segments: list[TimelineSegment] = []

        for line in stdout_text.splitlines():
            parsed = self._parse_json_line(line)
            if parsed is None:
                continue

            text = str(parsed.get("text", "")).strip()
            if not text:
                continue

            start = self._to_float(parsed.get("start"))
            end = self._to_float(parsed.get("end"))
            if end < start:
                end = start

            segments.append(TimelineSegment(start=start, end=end, text=text))

        if not segments:
            raise ASRProcessingError(
                "Paraformer command succeeded but no valid segments were produced.",
                details=self._tail_text(stdout_text),
            )

        return segments

    def _parse_emotion(self, stdout_text: str) -> str | None:
        emotions: list[str] = []

        for line in stdout_text.splitlines():
            parsed = self._parse_json_line(line)
            if parsed is None:
                continue

            raw_emotion = parsed.get("emotion")
            emotion = str(raw_emotion).strip() if raw_emotion is not None else ""
            if emotion:
                emotions.append(emotion)

        if not emotions:
            return None

        return Counter(emotions).most_common(1)[0][0]

    async def _save_upload_file(self, upload_file: UploadFile, target_path: Path) -> None:
        chunk_size = self._settings.storage.chunk_size

        try:
            with target_path.open("wb") as destination:
                while True:
                    chunk = await upload_file.read(chunk_size)
                    if not chunk:
                        break
                    destination.write(chunk)
        except OSError as exc:
            raise ASRProcessingError("Failed to persist uploaded audio file.") from exc
        finally:
            await upload_file.close()

    @staticmethod
    def _parse_json_line(line: str) -> dict | None:
        stripped = line.strip()
        if not stripped.startswith("{"):
            return None

        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return None

        if not isinstance(parsed, dict):
            return None

        return parsed

    @staticmethod
    def _to_float(value: object) -> float:
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _calc_speed(text: str, duration_sec: float) -> int:
        if duration_sec <= 0:
            return 0

        characters = len(text)
        speed = (characters * 60) / duration_sec
        return int(round(speed))

    @staticmethod
    def _normalize_language(language: str | None) -> str | None:
        if language is None:
            return None

        normalized = language.strip()
        return normalized or None

    @staticmethod
    def _resolve_working_dir(working_dir: str | None) -> str | None:
        if not working_dir:
            return None

        return str(Path(working_dir).expanduser().resolve())

    @staticmethod
    def _tail_text(text: str, *, lines: int = 20) -> str:
        split_lines = text.strip().splitlines()
        if not split_lines:
            return ""
        return "\n".join(split_lines[-lines:])

    def _build_temp_path(self, filename: str | None) -> Path:
        suffix = Path(filename or "audio.wav").suffix or ".wav"
        return self._tmp_dir / f"{uuid4().hex}{suffix}"

    async def _delete_temp_file(self, target_path: Path) -> None:
        if not target_path.exists():
            return

        try:
            target_path.unlink(missing_ok=True)
            self._logger.info("Temporary file removed: %s", target_path)
        except OSError as exc:
            self._logger.warning("Failed to remove temp file %s: %s", target_path, exc)

    async def _delete_temp_dir(self, target_dir: Path) -> None:
        if not target_dir.exists():
            return

        try:
            target_dir.rmdir()
            self._logger.info("Temporary directory removed: %s", target_dir)
        except OSError as exc:
            self._logger.warning("Failed to remove temp directory %s: %s", target_dir, exc)
