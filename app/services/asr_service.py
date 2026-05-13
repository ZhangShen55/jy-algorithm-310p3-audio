from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Literal
from uuid import uuid4

from fastapi import UploadFile

from app.core.config import ParaformerCliSettings, SenseVoiceCliSettings, Settings, resolved_config_dir
from app.core.errors import ASRProcessingError, ConcurrencyLimitError
from app.schemas.asr import AsrResponse, Segment


@dataclass(frozen=True)
class TimelineSegment:
    start: float
    end: float
    text: str
    emotion: str | None = None


@dataclass
class TaskInfo:
    task_id: str
    filename: str
    status: Literal["queued", "processing", "completed", "failed"]
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None


class ASRService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._logger = logging.getLogger(self.__class__.__name__)
        self._tmp_dir = Path(settings.storage.tmp_dir).expanduser().resolve()
        self._tmp_dir.mkdir(parents=True, exist_ok=True)

        # 并发控制
        self._semaphore = asyncio.Semaphore(settings.asr.max_concurrent_requests)
        self._queue: asyncio.Queue[tuple[str, asyncio.Event]] = asyncio.Queue(maxsize=settings.asr.max_queue_size)

        # 状态跟踪
        self._tasks: dict[str, TaskInfo] = {}
        self._tasks_lock = asyncio.Lock()
        self._success_count = 0
        self._failure_count = 0

        # 队列处理器任务（延迟启动）
        self._queue_processor_task: asyncio.Task | None = None

    async def start(self) -> None:
        """启动后台队列处理任务"""
        if self._queue_processor_task is None:
            self._queue_processor_task = asyncio.create_task(self._process_queue())
            self._logger.info("Queue processor started")

    async def stop(self) -> None:
        """停止后台队列处理任务"""
        if self._queue_processor_task is not None:
            self._queue_processor_task.cancel()
            try:
                await self._queue_processor_task
            except asyncio.CancelledError:
                pass
            self._logger.info("Queue processor stopped")

    async def transcribe(
        self,
        *,
        upload_file: UploadFile,
        show_emotion: bool,
        language: str | None,
        open_punc: bool,
    ) -> AsrResponse:
        task_id = uuid4().hex

        self._logger.info(
            "ASR request received. task_id=%s filename=%s show_emotion=%s language=%s open_punc=%s",
            task_id,
            upload_file.filename,
            show_emotion,
            language,
            open_punc,
        )

        # 尝试立即获取信号量（非阻塞）
        acquired = False
        if self._semaphore._value > 0:
            try:
                # 使用wait_for实现非阻塞尝试
                await asyncio.wait_for(self._semaphore.acquire(), timeout=0.001)
                acquired = True
                self._logger.info("ASR task acquired processing slot. task_id=%s", task_id)
            except asyncio.TimeoutError:
                acquired = False

        if not acquired:
            # 没有可用槽位，尝试加入队列（需要原子性检查和加入）
            try:
                # 使用put_nowait确保原子性，如果队列满会立即抛出QueueFull异常
                event = asyncio.Event()

                # 必须在锁内完成队列检查和入队，避免竞态条件
                async with self._tasks_lock:
                    # 先尝试入队（原子操作）
                    self._queue.put_nowait((task_id, event))

                    # 入队成功后再创建task记录
                    self._tasks[task_id] = TaskInfo(
                        task_id=task_id,
                        filename=upload_file.filename or "unknown",
                        status="queued",
                        created_at=datetime.now(),
                    )

                self._logger.info("ASR task queued. task_id=%s filename=%s queue_position=%d", task_id, upload_file.filename, self._queue.qsize())

                # 等待轮到自己
                await event.wait()

                self._logger.info("ASR task dequeued, starting processing. task_id=%s", task_id)

                # 获取信号量
                await self._semaphore.acquire()
            except asyncio.QueueFull:
                # 队列已满，清理已创建的task记录
                async with self._tasks_lock:
                    self._tasks.pop(task_id, None)

                raise ConcurrencyLimitError(
                    message="Server is busy and queue is full. Please try again later.",
                    details={
                        "max_concurrent": self._settings.asr.max_concurrent_requests,
                        "max_queue_size": self._settings.asr.max_queue_size,
                    },
                )

        # 更新状态为处理中
        async with self._tasks_lock:
            if task_id in self._tasks:
                self._tasks[task_id].status = "processing"
                self._tasks[task_id].started_at = datetime.now()
            else:
                self._tasks[task_id] = TaskInfo(
                    task_id=task_id,
                    filename=upload_file.filename or "unknown",
                    status="processing",
                    created_at=datetime.now(),
                    started_at=datetime.now(),
                )

        temp_audio_path: Path | None = None
        request_start = perf_counter()
        try:
            temp_audio_path = self._build_temp_path(upload_file.filename)
            normalized_language = self._normalize_language(language)

            try:
                load_start = perf_counter()
                await self._save_upload_file(upload_file, temp_audio_path)
                load_audio_time_ms = (perf_counter() - load_start) * 1000

                self._logger.info(
                    "Audio upload saved. task_id=%s source_name=%s temp_path=%s size_bytes=%s",
                    task_id,
                    upload_file.filename,
                    temp_audio_path,
                    temp_audio_path.stat().st_size if temp_audio_path.exists() else -1,
                )

                # 校验音频时长
                try:
                    with wave.open(str(temp_audio_path), 'rb') as wf:
                        frames = wf.getnframes()
                        rate = wf.getframerate()
                        duration = frames / float(rate)
                        if duration < 1.0:
                            raise ASRProcessingError(f"音频时长过短: {duration:.2f}秒，要求至少1秒")
                except wave.Error as e:
                    raise ASRProcessingError(f"无效的音频文件格式: {e}")

                gpu_start = perf_counter()

                if show_emotion:
                    timeline_segments = await self._run_sensevoice(temp_audio_path, normalized_language)
                else:
                    timeline_segments = await self._run_paraformer(temp_audio_path)

                gpu_time_ms = (perf_counter() - gpu_start) * 1000

                self._logger.info(
                    "ASR processing completed. task_id=%s filename=%s load_time_ms=%.2f gpu_time_ms=%.2f segments=%d",
                    task_id,
                    upload_file.filename,
                    load_audio_time_ms,
                    gpu_time_ms,
                    len(timeline_segments),
                )

                response_segments: list[Segment] = []
                merged_text_parts: list[str] = []
                for timeline in timeline_segments:
                    duration = max(0.0, timeline.end - timeline.start)
                    response_segments.append(
                        Segment(
                            segment_text=timeline.text,
                            bg=f"{timeline.start:.2f}",
                            ed=f"{timeline.end:.2f}",
                            speed=self._calc_speed(timeline.text, duration),
                            role=None,
                            emotion=timeline.emotion,
                        )
                    )
                    merged_text_parts.append(timeline.text)

                # 标点恢复
                merged_text = "".join(merged_text_parts)
                if open_punc and merged_text.strip():
                    punc_start = perf_counter()
                    merged_text = await self._restore_punctuation(merged_text)
                    punc_time_ms = (perf_counter() - punc_start) * 1000
                    self._logger.info(
                        "Punctuation restoration completed. task_id=%s punc_time_ms=%.2f",
                        task_id,
                        punc_time_ms,
                    )

                    # 将标点分配回各个 segment
                    response_segments = self._distribute_punctuation_to_segments(
                        response_segments,
                        merged_text
                    )

                # 开启情绪识别时，保证每个 segment 都有 emotion 字段（兜底为"平淡"）
                if show_emotion:
                    for seg in response_segments:
                        if not seg.emotion:
                            seg.emotion = self._DEFAULT_EMOTION

                # 标记成功
                async with self._tasks_lock:
                    if task_id in self._tasks:
                        self._tasks[task_id].status = "completed"
                        self._tasks[task_id].completed_at = datetime.now()
                    self._success_count += 1

                total_time_ms = (perf_counter() - request_start) * 1000
                self._logger.info(
                    "ASR request completed successfully. task_id=%s filename=%s total_time_ms=%.2f segments=%d",
                    task_id,
                    upload_file.filename,
                    total_time_ms,
                    len(response_segments),
                )

                return AsrResponse(
                    language=normalized_language or "auto",
                    segments=response_segments,
                    text=merged_text,
                    load_audio_time_ms=f"{load_audio_time_ms:.2f}",
                    gpu_time_ms=f"{gpu_time_ms:.2f}",
                )
            except Exception as e:
                # 标记失败
                async with self._tasks_lock:
                    if task_id in self._tasks:
                        self._tasks[task_id].status = "failed"
                        self._tasks[task_id].completed_at = datetime.now()
                        self._tasks[task_id].error = str(e)
                    self._failure_count += 1

                self._logger.error(
                    "ASR request failed. task_id=%s filename=%s error=%s",
                    task_id,
                    upload_file.filename,
                    str(e),
                    exc_info=True,
                )
                raise
            finally:
                # 确保删除临时文件
                if temp_audio_path is not None:
                    await self._delete_temp_file(temp_audio_path)
        finally:
            self._semaphore.release()

    async def _process_queue(self) -> None:
        """后台任务：处理等待队列"""
        while True:
            try:
                task_id, event = await self._queue.get()
                self._logger.info("Processing queued task %s", task_id)
                event.set()  # 通知任务可以开始处理
                self._queue.task_done()
            except Exception as e:
                self._logger.error("Queue processor error: %s", e)
                await asyncio.sleep(1)

    async def get_status(self) -> dict:
        """获取服务状态"""
        async with self._tasks_lock:
            processing_tasks = [
                {"task_id": t.task_id, "filename": t.filename, "started_at": t.started_at.isoformat()}
                for t in self._tasks.values()
                if t.status == "processing"
            ]
            queued_tasks = [
                {"task_id": t.task_id, "filename": t.filename, "created_at": t.created_at.isoformat()}
                for t in self._tasks.values()
                if t.status == "queued"
            ]

            return {
                "success_count": self._success_count,
                "failure_count": self._failure_count,
                "processing_count": len(processing_tasks),
                "queued_count": len(queued_tasks),
                "max_concurrent": self._settings.asr.max_concurrent_requests,
                "max_queue_size": self._settings.asr.max_queue_size,
                "available_slots": self._semaphore._value,
                "processing_tasks": processing_tasks,
                "queued_tasks": queued_tasks,
            }

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

    async def _run_sensevoice(self, audio_path: Path, language: str | None) -> list[TimelineSegment]:
        sensevoice_config = self._settings.asr.sensevoice
        command = self._build_sensevoice_command(audio_path, language, sensevoice_config)
        stdout_text = await self._run_command(
            command=command,
            timeout_seconds=sensevoice_config.command_timeout_seconds,
            working_dir=sensevoice_config.working_dir,
            command_label="sensevoice",
        )
        return self._parse_sensevoice_segments(stdout_text)

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

        env = self._subprocess_env_for_cli(working_dir)
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
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
            f"--num-threads={config.num_threads}",
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
            f"--num-threads={config.num_threads}",
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

            segments.append(TimelineSegment(start=start, end=end, text=text, emotion=None))

        if not segments:
            raise ASRProcessingError(
                "Paraformer command succeeded but no valid segments were produced.",
                details=self._tail_text(stdout_text),
            )

        return segments

    def _parse_sensevoice_segments(self, stdout_text: str) -> list[TimelineSegment]:
        segments: list[TimelineSegment] = []

        for line in stdout_text.splitlines():
            parsed = self._parse_json_line(line)
            if parsed is None:
                continue

            text = str(parsed.get("text", "")).strip()
            if not text:
                self._logger.warning("SenseVoice segment has empty text field. Raw data: %s", parsed)
                continue

            start = self._to_float(parsed.get("start"))
            end = self._to_float(parsed.get("end"))
            if end < start:
                end = start

            emotion_raw = parsed.get("emotion")
            emotion_str = self._map_emotion(str(emotion_raw).strip()) if emotion_raw is not None else None

            self._logger.debug("Parsed SenseVoice segment: text=%s, emotion=%s->%s, start=%s, end=%s", text, emotion_raw, emotion_str, start, end)
            segments.append(TimelineSegment(start=start, end=end, text=text, emotion=emotion_str))

        if not segments:
            raise ASRProcessingError(
                "SenseVoice command succeeded but no valid segments were produced.",
                details=self._tail_text(stdout_text),
            )

        return segments

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

    _SPEED_CALIBRATION_FACTOR: float = 0.6

    @staticmethod
    def _calc_speed(text: str, duration_sec: float) -> int:
        if duration_sec <= 0:
            return 0

        characters = len(text)
        raw_speed = (characters * 60) / duration_sec
        return int(round(raw_speed * ASRService._SPEED_CALIBRATION_FACTOR))

    _EMOTION_MAP: dict[str, str] = {
        "<|HAPPY|>": "积极",
        "<|SAD|>": "平淡",
        "<|ANGRY|>": "强调",
        "<|NEUTRAL|>": "平淡",
        "<|FEARFUL|>": "思考",
        "<|DISGUSTED|>": "疑问",
        "<|SURPRISED|>": "兴奋",
    }

    _DEFAULT_EMOTION: str = "平淡"

    @staticmethod
    def _map_emotion(raw_emotion: str) -> str | None:
        """将 SenseVoice 的情感标签映射为教学场景标签"""
        if not raw_emotion:
            return None
        return ASRService._EMOTION_MAP.get(raw_emotion.upper())

    def _distribute_punctuation_to_segments(
        self,
        segments: list[Segment],
        punctuated_text: str
    ) -> list[Segment]:
        """将标点分配回各个 segment

        策略：
        1. 先构建字符到 segment 的映射（每个非标点字符属于哪个 segment）
        2. 遍历 punctuated_text，将标点添加到前一个非标点字符所属的 segment

        Args:
            segments: 原始的 segment 列表（无标点）
            punctuated_text: 带标点的完整文本

        Returns:
            更新后的 segment 列表（segment_text 中包含标点）
        """
        if not segments or not punctuated_text:
            return segments

        # 定义标点符号集合
        punctuation_chars = set('，。！？；：、""''（）【】《》…—,.!?;:\'"()[]<>-')

        # 复制 segments，清空 segment_text，稍后重新构建
        result_segments = [
            Segment(
                segment_text="",
                bg=seg.bg,
                ed=seg.ed,
                speed=seg.speed,
                role=seg.role,
                emotion=seg.emotion,
            )
            for seg in segments
        ]

        # 构建原始文本（无标点）
        original_text = "".join(seg.segment_text for seg in segments)

        # 构建字符索引到 segment 索引的映射
        char_to_seg = []  # [(char, seg_idx), ...]
        for seg_idx, seg in enumerate(segments):
            for char in seg.segment_text:
                char_to_seg.append((char, seg_idx))

        # 遍历 punctuated_text，分配字符和标点
        orig_idx = 0  # 原始文本的索引
        last_seg_idx = -1  # 最后一个非标点字符所属的 segment

        for text_char in punctuated_text:
            if text_char in punctuation_chars:
                # 标点：添加到最后一个非标点字符所属的 segment
                if last_seg_idx >= 0:
                    result_segments[last_seg_idx].segment_text += text_char
            else:
                # 非标点字符：匹配原始文本
                if orig_idx < len(char_to_seg):
                    orig_char, seg_idx = char_to_seg[orig_idx]
                    if orig_char == text_char:
                        # 匹配成功
                        result_segments[seg_idx].segment_text += text_char
                        last_seg_idx = seg_idx
                        orig_idx += 1
                    else:
                        # 不匹配，尝试在接下来的字符中查找匹配
                        found = False
                        # 向前查找最多10个字符
                        for skip in range(1, min(11, len(char_to_seg) - orig_idx)):
                            if char_to_seg[orig_idx + skip][0] == text_char:
                                # 找到匹配，跳过中间的字符
                                orig_idx += skip
                                orig_char, seg_idx = char_to_seg[orig_idx]
                                result_segments[seg_idx].segment_text += text_char
                                last_seg_idx = seg_idx
                                orig_idx += 1
                                found = True
                                break

                        if not found:
                            # 仍然找不到，跳过这个字符（来自标点恢复的额外字符）
                            pass

        self._logger.debug(
            "Distributed punctuation to %d segments",
            len(result_segments)
        )

        return result_segments

    async def _restore_punctuation(self, text: str) -> str:
        """使用 sherpa-onnx-offline-punctuation 为文本添加标点"""
        config = self._settings.punctuation_cli

        command = [
            config.executable,
            f"--ct-transformer={config.ct_transformer}",
            text,
        ]

        self._logger.debug("Running punctuation command: %s", " ".join(command))

        cwd = self._resolve_working_dir(config.working_dir)
        env = self._subprocess_env_for_cli(config.working_dir)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
            stdout_bytes, stderr_bytes = await process.communicate()

            if process.returncode != 0:
                stderr_text = stderr_bytes.decode("utf-8", errors="replace")
                raise ASRProcessingError(
                    f"Punctuation restoration failed with exit code {process.returncode}",
                    details=self._tail_text(stderr_text),
                )

            result = stdout_bytes.decode("utf-8", errors="replace").strip()
            if not result:
                self._logger.warning("Punctuation restoration returned empty result, using original text")
                return text

            return result

        except FileNotFoundError as exc:
            raise ASRProcessingError(
                f"Punctuation CLI executable not found: {config.executable}"
            ) from exc
        except Exception as exc:
            self._logger.error("Punctuation restoration failed: %s", exc)
            # 标点恢复失败时返回原文本，不中断整个流程
            return text

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
        p = Path(working_dir).expanduser()
        if p.is_absolute():
            return str(p.resolve())
        # 相对路径：相对 config.toml 所在目录（与 sherpa 工程并列时可用 ../sherpa-onnx/build）
        return str((resolved_config_dir() / p).resolve())

    @staticmethod
    def _cli_library_dirs(resolved_working_dir: str | None) -> list[Path]:
        """sherpa-onnx 可执行文件依赖的 .so 常见位置（相对 CMake build 目录）。"""
        if not resolved_working_dir:
            return []
        base = Path(resolved_working_dir)
        dirs: list[Path] = []
        for rel in (Path("lib"), Path("_deps/onnxruntime-src/lib")):
            p = base / rel
            if p.is_dir():
                dirs.append(p)
        deps = base / "_deps"
        if deps.is_dir():
            for child in sorted(deps.iterdir()):
                cand = child / "lib"
                if cand.is_dir() and (cand / "libonnxruntime.so").is_file() and cand not in dirs:
                    dirs.append(cand)
        return dirs

    @staticmethod
    def _subprocess_env_for_cli(working_dir: str | None) -> dict[str, str]:
        """为 CLI 子进程补齐 LD_LIBRARY_PATH，避免 uvicorn 环境下找不到 libonnxruntime.so。"""
        env = dict(os.environ)
        cwd = ASRService._resolve_working_dir(working_dir)
        extra_dirs = ASRService._cli_library_dirs(cwd)
        if not extra_dirs:
            return env
        extra = ":".join(str(p) for p in extra_dirs)
        existing = env.get("LD_LIBRARY_PATH", "").strip()
        env["LD_LIBRARY_PATH"] = f"{extra}:{existing}" if existing else extra
        return env

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
