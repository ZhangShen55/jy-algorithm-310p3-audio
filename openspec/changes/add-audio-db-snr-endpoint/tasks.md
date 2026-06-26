## 1. Configuration and Schemas

- [x] 1.1 Add `AudioMetricsSettings` to `app/core/config.py` with defaults for FFmpeg executable, decode sample rate, analysis frame size, dB floor, and decode timeout.
- [x] 1.2 Add `AudioMetricItem` and `AudioMetricsResponse` models in `app/schemas/audio_metrics.py` with `extra="forbid"` and the exact response fields from the contract.
- [x] 1.3 Add or reuse a clear processing error type for audio metrics failures and decide route mappings for validation, decode, and unexpected processing errors.

## 2. Core Audio Metrics Implementation

- [x] 2.1 Implement a pure `AudioMetricsAnalyzer.analyze_wave(path, time_size)` that reads mono PCM WAV data, splits it into `time_size` windows, and returns per-window metric items.
- [x] 2.2 Implement dBFS calculation, dB flooring, `0..100` volume mapping, and two-decimal rounding for `max_db`, `db`, and `min_db`.
- [x] 2.3 Implement estimated SNR from high-energy and low-energy analysis frames, including silent and near-silent edge cases.
- [x] 2.4 Implement FFmpeg decoding to mono 16 kHz signed 16-bit WAV with timeout, stderr tail logging, and clear errors for missing executable or decode failure.
- [x] 2.5 Implement `AudioMetricsService.analyze(upload_file, time_size)` to stream uploads to temp files, call decode and analyzer, build `task_id`, `process_time_ms`, and `timestamp`, and clean all temp files in `finally`.

## 3. API Integration

- [x] 3.1 Add `get_audio_metrics_service()` in `app/api/deps.py` using the existing cached dependency style.
- [x] 3.2 Add `app/api/routes/audio_metrics.py` with `POST /audio/db_snr`, multipart form parsing, filename validation, `time_size` validation, response model binding, and error handling.
- [x] 3.3 Include the audio metrics router in `app/api/router.py` without changing the existing ASR or health routes.
- [x] 3.4 Add the optional `[audio_metrics]` example block to `config.toml` or document that defaults work when `ffmpeg` is on `PATH`.

## 4. Verification

- [x] 4.1 Add analyzer unit tests using generated PCM WAV fixtures for window splitting, partial final windows, dB ordering, and volume mapping.
- [x] 4.2 Add silent-audio and low-energy tests covering dB flooring and `snr=0.0` behavior.
- [x] 4.3 Add schema tests for exact response serialization and rejection of extra fields.
- [x] 4.4 Add service or route tests that verify invalid `time_size`, empty filename, decode failure handling, and temp-file cleanup.
- [x] 4.5 Run `python -m unittest test/test_audio_metrics_service.py` and `python -m compileall app` before marking the change complete.
