## Why

The service needs a lightweight audio quality endpoint that reports volume, dBFS, and estimated SNR before or alongside ASR. This should be portable across Huawei 310P3 deployments without coupling the feature to NPU model execution or requiring per-server code changes.

## What Changes

- Add a new multipart `POST /audio/db_snr` API that accepts `audioFile` and `time_size`.
- Support common uploaded audio formats from the interface contract: WAV, MP3, and AAC, with decoding handled by a configurable FFmpeg executable.
- Return per-window metrics: `bg`, `ed`, `max_db`, `db`, `min_db`, `snr`, `max_volume`, `volume`, and `min_volume`.
- Return top-level `task_id`, `process_time_ms`, and Unix `timestamp`.
- Keep the ASR transcription pipeline unchanged.
- Treat SNR as an estimated single-file metric based on low-energy frame noise-floor detection, not as calibrated lab-grade SNR requiring a separate clean/noise reference.

## Capabilities

### New Capabilities
- `audio-db-snr-detection`: Defines the audio quality detection endpoint, request contract, response contract, metric calculation semantics, and failure behavior.

### Modified Capabilities

None.

## Impact

- Affected API surface: new `POST /audio/db_snr` route.
- Affected code areas: FastAPI routing, dependency wiring, Pydantic response schemas, a new audio quality service, config loading, and tests.
- Runtime dependency: FFmpeg must be available on the server path or configured via `config.toml`; Python ML runtimes remain unnecessary.
- Operational impact: quality detection runs outside the ASR semaphore/queue so lightweight audio analysis does not block NPU-backed ASR requests.
