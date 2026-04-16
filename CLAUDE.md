# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Seacraft ASR (Automatic Speech Recognition) FastAPI service that provides a two-stage audio transcription pipeline:

1. **Stage 1 (Paraformer)**: Generates text transcription with timestamps (start/end/text)
2. **Stage 2 (SenseVoice)**: Extracts emotion tags for each segment by splitting audio based on Stage 1 timestamps

Both ASR engines run as external CLI executables (`sherpa-onnx-vad-with-offline-asr`) configured to use Ascend NPU (310P3) hardware acceleration.

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
  -F "showEmotion=true"
```

Health check:
```bash
curl http://127.0.0.1:8081/healthz
```

## Architecture

### Two-Stage ASR Pipeline

The service orchestrates two separate ASR commands:

- **Paraformer** (`config.toml` → `[asr.paraformer]`): Produces timeline segments with text. The final response `text` field comes entirely from Paraformer output.
- **SenseVoice** (`config.toml` → `[asr.sensevoice]`): Runs on audio chunks split by Paraformer timestamps. Only the `emotion` field is extracted from SenseVoice output (text is discarded).

### Key Flow (app/services/asr_service.py)

1. Upload audio → save to `tmp_audio/` directory
2. Run Paraformer on full audio → parse JSON output for `{start, end, text}` segments
3. If `showEmotion=true`: split audio into WAV chunks using `wave` module based on Paraformer timestamps
4. Run SenseVoice on each chunk → parse JSON output for `emotion` field
5. Merge results: Paraformer text + SenseVoice emotions → response segments
6. Clean up all temporary files (uploaded audio + split chunks)

### External Command Execution

Both ASR engines are invoked via `asyncio.create_subprocess_exec`:
- Commands run in `working_dir` specified in config (allows relative model paths)
- Stdout is parsed line-by-line as JSON objects
- Stderr is logged as warnings if non-empty
- Timeouts are configurable per engine (`command_timeout_seconds`)

### Audio Splitting Logic

Audio splitting uses Python's `wave` module to read PCM WAV files and extract frame ranges. The service expects uploaded audio to be WAV format. If audio is not PCM WAV, splitting will fail with `ASRProcessingError`.

## Configuration (config.toml)

The service requires two complete ASR configurations:

- `[asr.paraformer]`: Must include `executable`, `working_dir`, `provider`, VAD settings, `paraformer` model paths, and `tokens`
- `[asr.sensevoice]`: Must include `executable`, `working_dir`, `provider`, VAD settings, `sense_voice_model` path, and `tokens`

Both configurations support:
- `working_dir`: Commands execute in this directory, enabling relative model paths
- `command_timeout_seconds`: Per-command timeout (default 7200s / 2 hours)
- `provider`: Hardware acceleration provider (e.g., "ascend" for NPU)

## API Endpoint

`POST /v1.1.8/seacraft_asr` (form-data):
- `audioFile` (File, required): Audio file to transcribe
- `showSpk` (bool, default=false): Speaker diarization flag (not implemented, logs warning)
- `showEmotion` (bool, default=true): Whether to extract emotion tags via SenseVoice
- `language` (string, optional): Target language for SenseVoice (e.g., "zh", "en", "auto")

Response includes:
- `segments[]`: Array of `{segment_text, bg, ed, speed, emotion}`
- `text`: Concatenated full transcription (from Paraformer only)
- `language`: Normalized language parameter or "auto"
- `load_audio_time_ms`: Time to save uploaded file
- `gpu_time_ms`: Combined time for both ASR stages

## Important Behaviors

- **Text source**: The response `text` field is built exclusively from Paraformer output. SenseVoice only contributes `emotion` tags.
- **Emotion extraction**: When `showEmotion=false`, the service skips audio splitting and SenseVoice execution entirely. All segments will have `emotion: null`.
- **Temporary files**: All uploaded audio and split chunks are deleted after processing, even if errors occur.
- **Speaker diarization**: `showSpk=true` is accepted but not implemented. A warning is logged and the parameter is ignored.
- **Speed calculation**: `speed` field represents characters per minute: `(len(text) * 60) / duration_seconds`
- **Emotion voting**: If a SenseVoice chunk produces multiple emotion tags, the most common one is selected via `Counter.most_common(1)`.

## Error Handling

- `ASRProcessingError`: Custom exception for ASR pipeline failures (app/core/errors.py)
- Command failures (non-zero exit code) raise `ASRProcessingError` with stderr/stdout tail
- Audio splitting errors (non-WAV format) raise `ASRProcessingError` with wave.Error details
- SenseVoice failures per chunk are logged as warnings but don't fail the entire request (emotion becomes `null` for that segment)
- Unhandled exceptions are caught by global handler and return 500 with generic error message

## Dependencies

- FastAPI + Uvicorn for HTTP server
- Pydantic for config/schema validation
- python-multipart for form-data uploads
- tomli for TOML config parsing (Python <3.11)
- No ML libraries (PyTorch, ONNX, etc.) — ASR runs as external processes
