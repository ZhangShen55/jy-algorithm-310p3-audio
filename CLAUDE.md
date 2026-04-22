# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Seacraft ASR (Automatic Speech Recognition) FastAPI service that orchestrates three external CLI engines on Ascend 310P3 NPU hardware:

1. **Paraformer** (`sherpa-onnx-vad-with-offline-asr`): plain ASR with word/segment timestamps
2. **SenseVoice** (`sherpa-onnx-vad-with-offline-asr`): multilingual ASR + emotion tagging (zh/en/ja/ko/yue)
3. **CT-Transformer Punctuation** (`sherpa-onnx-offline-punctuation`): offline punctuation restoration

The FastAPI process itself ships no ML libraries (no PyTorch / ONNX Runtime in `requirements.txt`). All model execution happens in child processes launched via `asyncio.create_subprocess_exec`; the service only parses their JSON stdout and merges results.

## Running the Service

Start the service:
```bash
python run.py
```

Or directly with uvicorn:
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8081
```

To use a custom config file:
```bash
APP_CONFIG_FILE=/path/to/config.toml python run.py
```

Test the API:
```bash
curl -X POST "http://127.0.0.1:8081/v1.1.8/seacraft_asr" \
  -F "audioFile=@/path/to/audio.wav" \
  -F "showSpk=false" \
  -F "showEmotion=true" \
  -F "openPunc=true"
```

Health check / runtime status:
```bash
curl http://127.0.0.1:8081/healthz
curl http://127.0.0.1:8081/get_status
```

## Architecture

### Pipeline (either/or, not two-stage)

Despite the legacy name, the current pipeline is **not** a Paraformer → SenseVoice chain. `ASRService.transcribe()` picks **one** ASR engine based on `showEmotion`, then optionally runs punctuation restoration on top:

- `showEmotion=true` → run **SenseVoice** once on the full audio. SenseVoice already returns `{start, end, text, emotion}` per segment, so no audio splitting is performed.
- `showEmotion=false` → run **Paraformer** once on the full audio. All segments get `emotion: null`.
- `openPunc=true` (default) → take the concatenation of all segment texts, feed it to the CT-Transformer punctuation CLI, and redistribute the returned punctuation back into the original segments (see `_distribute_punctuation_to_segments`).

### Key Flow (`app/services/asr_service.py`)

1. Acquire a processing slot via `asyncio.Semaphore` (`max_concurrent_requests`); if full, enqueue on `asyncio.Queue` (`max_queue_size`); if the queue is also full, raise `ConcurrencyLimitError` → HTTP 429.
2. Save the upload to `tmp_audio/<uuid>.<ext>`.
3. Validate the audio with the stdlib `wave` module (must be PCM WAV, duration ≥ 1.0s).
4. Run **SenseVoice** *or* **Paraformer** (one shot, full audio).
5. Build `Segment` objects and compute `speed = round(len(text) * 60 / duration_sec)` per segment.
6. If `openPunc=true` and the merged text is non-empty: call the punctuation CLI and redistribute punctuation back onto segments.
7. Map SenseVoice raw emotion tags to teaching-domain Chinese labels (see "Emotion mapping" below).
8. Clean up the temporary audio file and release the semaphore.

### External Command Execution

All three engines are invoked the same way:

- Launched via `asyncio.create_subprocess_exec` in the `working_dir` from config (so model paths can be relative).
- Stdout is parsed line-by-line; only lines beginning with `{` are attempted as JSON. Anything else is ignored.
- Non-empty stderr is logged as a warning (tail only).
- Non-zero exit codes raise `ASRProcessingError` with the tail of stderr/stdout.
- Paraformer / SenseVoice timeouts are configurable per engine (`command_timeout_seconds`, default 7200s). The punctuation CLI has no explicit timeout wrapper.

### Concurrency & Queue Model

`ASRService` owns an application-level admission layer:

- `asyncio.Semaphore(settings.asr.max_concurrent_requests)` caps concurrent ASR executions.
- `asyncio.Queue(maxsize=settings.asr.max_queue_size)` holds waiters when the semaphore is exhausted.
- A background task started in the FastAPI `lifespan` (`_process_queue`) wakes waiters one at a time via `asyncio.Event`.
- Per-request `TaskInfo` records (`queued / processing / completed / failed`) plus `success_count` / `failure_count` are maintained under `_tasks_lock` and exposed through `GET /get_status`.
- Queue-full is surfaced as `ConcurrencyLimitError` → HTTP 429.

### Emotion Mapping

SenseVoice emits raw English emotion labels. `ASRService._EMOTION_MAP` rewrites them to teaching-scenario Chinese tags before returning to the caller:

| SenseVoice raw | API `emotion` field |
|---|---|
| `HAPPY` | `积极` |
| `SAD` | `平淡` |
| `ANGRY` | `强调` |
| `NEUTRAL` | `平淡` |
| `FEARFUL` | `思考` |
| `DISGUSTED` | `疑问` |
| `SURPRISED` | `兴奋` |

Unknown labels become `null`.

### Punctuation Redistribution

`_restore_punctuation` shells out to `sherpa-onnx-offline-punctuation` with the entire concatenated text as a single argv argument and reads one punctuated line from stdout. If the CLI fails or returns empty, the service logs a warning and **falls back to the original (un-punctuated) text** rather than erroring out.

`_distribute_punctuation_to_segments` then walks the punctuated text character by character, matching non-punctuation characters against the original segments' character stream (with up to a 10-character forward lookahead to tolerate recognition diffs) and attaching each punctuation mark to the segment that owns the preceding non-punctuation character. This means both `response.text` (punctuated, full) and `response.segments[*].segment_text` (punctuated, per-segment) stay consistent.

## Configuration (`config.toml`)

The service requires three CLI configurations plus the standard server/storage/logging blocks:

- `[asr.paraformer]` — executable + `working_dir` + `provider` (e.g. `"ascend"`) + `num_threads` (forwarded as `--num-threads=…`, default 2) + VAD settings (`silero_vad_model`, `silero_vad_threshold`, `silero_vad_min_silence_duration`) + `paraformer` (comma-separated encoder/predictor/decoder `.om` paths) + `tokens` + `command_timeout_seconds`
- `[asr.sensevoice]` — same runtime knobs (`provider`, `num_threads`, VAD), plus `sense_voice_model` (single `.om`) and `tokens`
- `[punctuation_cli]` — `executable`, `working_dir`, `ct_transformer` (path to the int8 ONNX punctuation model)
- `[asr]` — `max_concurrent_requests` (semaphore size) and `max_queue_size` (waiting queue size)

`working_dir` lets both ASR and punctuation commands run with relative model paths.

## API Endpoints

### `POST /v1.1.8/seacraft_asr` (multipart form)

| Field | Type | Default | Purpose |
|---|---|---|---|
| `audioFile` | File | required | PCM WAV (≥ 1.0s) |
| `showSpk` | bool | `false` | Speaker diarization flag (**not implemented**, only logs a warning) |
| `showEmotion` | bool | `true` | `true` → SenseVoice, `false` → Paraformer |
| `language` | string | `null` | Forwarded to SenseVoice as `--sense-voice-language=…` (e.g. `zh`, `en`, `auto`); ignored when running Paraformer |
| `openPunc` | bool | `true` | Whether to run CT-Transformer punctuation restoration |

Response (`AsrResponse`, `extra="forbid"`):

- `language`: normalized input language, or `"auto"` if not provided
- `segments[]`: `{segment_text, bg, ed, speed, role, emotion}`
  - `bg` / `ed` are seconds formatted to 2 decimals (strings)
  - `speed` is characters per minute: `round(len(text) * 60 / duration_sec)`, `0` when duration ≤ 0
  - `role` is always `null` (reserved for diarization)
  - `emotion` is a mapped Chinese label or `null`
- `text`: concatenated segment texts (punctuated if `openPunc=true`)
- `load_audio_time_ms` / `gpu_time_ms`: timing strings formatted to 2 decimals

### `GET /get_status`

Returns live counters and per-task snapshots:

```json
{
  "success_count": 0,
  "failure_count": 0,
  "processing_count": 0,
  "queued_count": 0,
  "max_concurrent": 20,
  "max_queue_size": 10,
  "available_slots": 20,
  "processing_tasks": [],
  "queued_tasks": []
}
```

### `GET /healthz`

Returns `{"status": "ok"}`.

## Important Behaviors

- **Engine selection by `showEmotion`**: there is no longer a Paraformer-then-split-then-SenseVoice flow. The response `text` comes from whichever engine was selected, post-punctuation if enabled.
- **Audio format**: only PCM WAV is supported. Non-WAV inputs and WAVs shorter than 1.0s raise `ASRProcessingError` → HTTP 500.
- **Temporary files**: the saved upload under `tmp_audio/` is always deleted, even on error (see the `finally` block in `transcribe`).
- **Speaker diarization**: `showSpk=true` is accepted but only logs a warning; the parameter is ignored.
- **Punctuation failure is non-fatal**: if the punctuation CLI errors or returns empty, `_restore_punctuation` returns the original text and processing continues.
- **Request tracing**: every HTTP request gets an `X-Request-ID` response header; the same ID is logged by the `request_log_middleware` in `app/main.py`.
- **CPython internal**: `transcribe()` probes `self._semaphore._value` for a fast "any slot free?" check before doing a non-blocking `acquire`. This is a CPython implementation detail and should be watched on Python upgrades.

## Error Handling

- `ASRProcessingError` (`app/core/errors.py`) — ASR / punctuation / audio validation failures. Mapped to HTTP 500 by `app/api/routes/asr.py`.
- `ConcurrencyLimitError` (subclass of `ASRProcessingError`) — raised when both the semaphore and the waiting queue are full. Mapped to HTTP 429.
- Command timeouts (`asyncio.wait_for`) kill the subprocess and raise `ASRProcessingError("... command timed out.")`.
- Per-segment SenseVoice lines with empty `text` are skipped with a warning; if **no** segments survive, a final `ASRProcessingError` is raised with the stdout tail.
- A global FastAPI exception handler catches anything else and returns 500 with a generic `"Internal server error."` body.

## Dependencies

- FastAPI 0.115 + Uvicorn 0.34 for the HTTP server
- Pydantic 2.11 for config and response schemas (`extra="forbid"`)
- `python-multipart` for form-data uploads
- `tomli` on Python < 3.11; stdlib `tomllib` otherwise
- `aiohttp` (used by the bench/test scripts in the repo root, not by the service itself)

No ML runtimes are required in the Python environment — Paraformer, SenseVoice and the CT-Transformer punctuation model run as external `sherpa-onnx-*` CLIs pointed at `.om` / `.onnx` artifacts on disk.
