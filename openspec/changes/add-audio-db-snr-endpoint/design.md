## Context

The current service is a FastAPI application that keeps model execution in external CLI processes and keeps Python dependencies small. ASR requests save uploads under `storage.tmp_dir`, run the selected sherpa-onnx CLI, parse JSON stdout, and expose results through Pydantic response models. The new audio dB/SNR endpoint is a separate quality check and does not need NPU resources or ASR model execution.

The interface contract requires `POST /audio/db_snr` with multipart `audioFile` and integer `time_size`, returning per-window dB, volume, and SNR metrics plus `task_id`, processing time, and timestamp. The screenshot lists AAC, WAV, and MP3 as expected upload formats.

## Goals / Non-Goals

**Goals:**

- Add a portable audio quality endpoint that works on 310P3 deployment machines without changing ASR behavior.
- Decode WAV, MP3, and AAC through a configurable FFmpeg executable and analyze normalized PCM in Python.
- Keep the response fields and types aligned with the provided contract.
- Define deterministic, testable metric semantics for dBFS, volume, and estimated SNR.
- Clean up temporary files on success and failure.

**Non-Goals:**

- Do not implement calibrated laboratory SNR that requires a separate reference/noise sample.
- Do not add PyTorch, ONNX Runtime, librosa, or other ML/audio-heavy Python dependencies.
- Do not add diarization, ASR transcription, or punctuation behavior to this endpoint.
- Do not reuse the ASR semaphore/queue unless later load testing proves audio analysis needs admission control.

## Decisions

1. **Use a new `AudioMetricsService` instead of extending `ASRService`.**

   The endpoint is not part of ASR model inference, so it should live in its own service, schema, dependency, and route module, for example `app/services/audio_metrics_service.py`, `app/schemas/audio_metrics.py`, and `app/api/routes/audio_metrics.py`. This keeps the existing `/v1.1.8/seacraft_asr` behavior stable and avoids mixing lightweight CPU analysis with NPU-backed request tracking.

   Alternatives considered:

   - Reuse `ASRService`: fewer files, but it couples quality checks to ASR queue/status and makes the already large service harder to maintain.
   - Add helpers directly in the route: quickest initially, but metric logic becomes hard to unit test.

2. **Decode with FFmpeg, analyze with Python.**

   The easiest portable path is to shell out to FFmpeg:

   ```text
   ffmpeg -hide_banner -nostdin -y -i <upload> -ac 1 -ar 16000 -sample_fmt s16 <decoded.wav>
   ```

   The analyzer then reads the decoded WAV with the stdlib `wave` module and computes metrics from signed 16-bit samples. Add an `[audio_metrics]` config block with defaults such as:

   - `ffmpeg_executable = "ffmpeg"`
   - `decode_sample_rate = 16000`
   - `analysis_frame_ms = 100`
   - `db_floor = -60.0`
   - `command_timeout_seconds = 300`

   This means a new server normally only needs FFmpeg on `PATH`; if the binary path differs, only `config.toml` changes.

   Alternatives considered:

   - Python-only WAV support: smallest dependency surface, but it fails MP3/AAC from the contract.
   - FFmpeg `astats` parsing: avoids sample loops, but stderr parsing is brittle and harder to test.
   - Add numpy/librosa: convenient math APIs, but adds avoidable deployment weight.

3. **Use explicit metric semantics.**

   The decoded mono PCM stream is split into windows of `time_size` seconds. The final partial window is included when it contains samples.

   Within each window:

   - `db` is the dBFS value of the whole-window RMS.
   - `max_db` and `min_db` are the max/min dBFS values across short analysis frames, defaulting to 100 ms.
   - dBFS is `20 * log10(rms / 32768)`, rounded to two decimals, and floored at `db_floor` for silence and near-silence.
   - `volume`, `max_volume`, and `min_volume` map dBFS to `0..100` with `round(clamp((db - db_floor) / (0 - db_floor), 0, 1) * 100)`. With the default `db_floor=-60`, `-17.3 dBFS` maps to `71`, matching the sample response style.
   - `snr` is an estimated single-file SNR: compute frame powers, treat the quietest 10% as the local noise floor and the loudest 10% as signal energy, then return `10 * log10(signal_power / noise_power)` clamped to `>= 0` and rounded to two decimals. If the window has no meaningful energy, return `0.0`.

4. **Keep request and response handling consistent with the current API style.**

   The route accepts `audioFile: UploadFile = File(...)` and `time_size: int = Form(...)`. It rejects an empty filename and non-positive `time_size` before processing. The response model uses `extra="forbid"` like `AsrResponse`.

   `task_id` should be generated as `task-<8 hex chars>_<original filename>` to match the provided response shape while still being unique enough for logs. `process_time_ms` is an integer elapsed time for the endpoint. `timestamp` is `int(time.time())` at response creation.

5. **Add focused tests around pure metric logic and API shape.**

   The analyzer should expose a pure `analyze_wave(path, time_size)` path so tests can generate tiny PCM WAV fixtures without requiring FFmpeg. Route/service tests can mock or bypass the decode step. This keeps verification reliable on development machines that may not have FFmpeg installed.

## Risks / Trade-offs

- **FFmpeg missing on a deployment machine** -> Keep `ffmpeg_executable` configurable and return a clear processing error when the binary is unavailable.
- **Estimated SNR may not match third-party tools exactly** -> Document the low-energy/high-energy frame method in the spec and tests; do not present it as calibrated SNR.
- **Very large uploads can consume CPU and disk while decoding** -> Reuse `storage.tmp_dir`, stream upload chunks, add a decode timeout, and clean up temp files in `finally`.
- **Stereo or unusual sample formats can produce inconsistent metrics** -> Normalize every input to mono, 16 kHz, signed 16-bit PCM before analysis.
- **Silence can create `-inf` dB values** -> Floor reported dB at `db_floor` and return `0` volume/SNR for silent windows.

## Migration Plan

1. Add the new config model with defaults so existing `config.toml` remains valid.
2. Add schemas, analyzer/service, route, and dependency wiring.
3. Include the new route in `app/api/router.py`.
4. Add tests for schema exactness, window splitting, dB-to-volume mapping, SNR edge cases, cleanup behavior, and route validation.
5. Deploy with FFmpeg available on `PATH` or set `[audio_metrics].ffmpeg_executable` in `config.toml`.

Rollback is simple: remove the route include or revert the change; no existing ASR endpoint or response contract is changed.

## Open Questions

None for the first implementation. If a downstream consumer later requires a specific vendor SNR formula, the analyzer can add a named `snr_method` config without changing the endpoint shape.
