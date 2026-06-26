from __future__ import annotations

import math
import struct
import tempfile
import unittest
import wave
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from app.api.deps import get_audio_metrics_service
from app.api.routes.audio_metrics import router as audio_metrics_router
from app.core.config import (
    AppSettings,
    AsrPipelineSettings,
    AudioMetricsSettings,
    LoggingSettings,
    ParaformerCliSettings,
    PunctuationCliSettings,
    SenseVoiceCliSettings,
    ServerSettings,
    Settings,
    StorageSettings,
)
from app.core.errors import AudioMetricsProcessingError
from app.schemas.audio_metrics import AudioMetricItem, AudioMetricsResponse
from app.services.audio_metrics_service import AudioMetricsAnalyzer, AudioMetricsAnalyzerConfig, AudioMetricsService


class AudioMetricsAnalyzerTests(unittest.TestCase):
    def _write_sine_wave(self, path: Path, *, duration_seconds: float, sample_rate: int = 16000) -> None:
        total_samples = int(duration_seconds * sample_rate)
        frames = bytearray()
        for index in range(total_samples):
            amplitude = 12000 if index < sample_rate * 2 else 4000
            sample = int(amplitude * math.sin(2 * math.pi * 440 * index / sample_rate))
            frames.extend(struct.pack("<h", sample))

        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(bytes(frames))

    def test_splits_pcm_wave_into_time_size_windows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            wav_path = Path(tmp_dir) / "sample.wav"
            self._write_sine_wave(wav_path, duration_seconds=3.0)

            analyzer = AudioMetricsAnalyzer(
                AudioMetricsAnalyzerConfig(
                    analysis_frame_ms=100,
                    db_floor=-60.0,
                )
            )
            metrics = analyzer.analyze_wave(wav_path, time_size=2)

        self.assertEqual(len(metrics), 2)
        self.assertEqual(metrics[0].bg, 0.0)
        self.assertEqual(metrics[0].ed, 2.0)
        self.assertEqual(metrics[1].bg, 2.0)
        self.assertEqual(metrics[1].ed, 3.0)

        for item in metrics:
            self.assertGreaterEqual(item.max_db, item.db)
            self.assertGreaterEqual(item.db, item.min_db)
            self.assertGreaterEqual(item.max_volume, item.volume)
            self.assertGreaterEqual(item.volume, item.min_volume)
            self.assertEqual(item.volume, analyzer.db_to_volume(item.db))

    def test_rejects_non_positive_time_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            wav_path = Path(tmp_dir) / "sample.wav"
            self._write_sine_wave(wav_path, duration_seconds=1.0)

            analyzer = AudioMetricsAnalyzer()

            with self.assertRaisesRegex(ValueError, "time_size must be greater than 0"):
                analyzer.analyze_wave(wav_path, time_size=0)

    def test_silent_audio_uses_db_floor_and_zero_snr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            wav_path = Path(tmp_dir) / "silent.wav"
            with wave.open(str(wav_path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(b"\x00\x00" * 16000)

            analyzer = AudioMetricsAnalyzer(
                AudioMetricsAnalyzerConfig(
                    analysis_frame_ms=100,
                    db_floor=-60.0,
                )
            )
            metrics = analyzer.analyze_wave(wav_path, time_size=1)

        self.assertEqual(len(metrics), 1)
        self.assertEqual(metrics[0].max_db, -60.0)
        self.assertEqual(metrics[0].db, -60.0)
        self.assertEqual(metrics[0].min_db, -60.0)
        self.assertEqual(metrics[0].snr, 0.0)
        self.assertEqual(metrics[0].max_volume, 0)
        self.assertEqual(metrics[0].volume, 0)
        self.assertEqual(metrics[0].min_volume, 0)


class AudioMetricsSchemaTests(unittest.TestCase):
    def test_response_schema_uses_exact_payload_fields(self) -> None:
        item = AudioMetricItem(
            bg=0.0,
            ed=10.0,
            max_db=-9.43,
            db=-17.3,
            min_db=-32.93,
            snr=45.34,
            max_volume=84,
            volume=71,
            min_volume=45,
        )
        response = AudioMetricsResponse(
            result=[item],
            task_id="task-287243d8_audio.mp3",
            process_time_ms=1733,
            timestamp=1748249265,
        )

        self.assertEqual(
            response.model_dump(),
            {
                "result": [
                    {
                        "bg": 0.0,
                        "ed": 10.0,
                        "max_db": -9.43,
                        "db": -17.3,
                        "min_db": -32.93,
                        "snr": 45.34,
                        "max_volume": 84,
                        "volume": 71,
                        "min_volume": 45,
                    }
                ],
                "task_id": "task-287243d8_audio.mp3",
                "process_time_ms": 1733,
                "timestamp": 1748249265,
            },
        )

    def test_response_schema_rejects_extra_fields(self) -> None:
        with self.assertRaises(ValidationError):
            AudioMetricsResponse(
                result=[],
                task_id="task-287243d8_audio.mp3",
                process_time_ms=1733,
                timestamp=1748249265,
                extra_field=True,
            )


def _settings_for_tmp_dir(tmp_dir: str) -> Settings:
    return Settings(
        app=AppSettings(),
        server=ServerSettings(),
        logging=LoggingSettings(),
        storage=StorageSettings(tmp_dir=tmp_dir, chunk_size=8),
        asr=AsrPipelineSettings(
            paraformer=ParaformerCliSettings(
                executable="paraformer",
                silero_vad_model="vad.onnx",
                paraformer="encoder.om,predictor.om,decoder.om",
                tokens="tokens.txt",
            ),
            sensevoice=SenseVoiceCliSettings(
                executable="sensevoice",
                silero_vad_model="vad.onnx",
                sense_voice_model="sensevoice.om",
                tokens="tokens.txt",
            ),
        ),
        punctuation_cli=PunctuationCliSettings(
            executable="punc",
            ct_transformer="punc.onnx",
        ),
        audio_metrics=AudioMetricsSettings(
            ffmpeg_executable="ffmpeg",
            command_timeout_seconds=1,
        ),
    )


class FailingDecodeAudioMetricsService(AudioMetricsService):
    async def _decode_to_wave(self, source_path: Path, target_path: Path) -> None:
        raise AudioMetricsProcessingError("decode failed")


class SuccessfulDecodeAudioMetricsService(AudioMetricsService):
    async def _decode_to_wave(self, source_path: Path, target_path: Path) -> None:
        with wave.open(str(target_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(b"\x01\x00" * 16000)


class AnalyzerFailureAudioMetricsService(AudioMetricsService):
    async def _decode_to_wave(self, source_path: Path, target_path: Path) -> None:
        target_path.write_bytes(b"decoded audio placeholder")

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings=settings)
        self._analyzer = self

    def analyze_wave(self, audio_path: Path, *, time_size: int) -> list[AudioMetricItem]:
        raise AudioMetricsProcessingError("analysis failed")


class AudioMetricsServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleans_temp_files_after_successful_processing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = SuccessfulDecodeAudioMetricsService(settings=_settings_for_tmp_dir(tmp_dir))
            upload = UploadFile(filename="sample.wav", file=BytesIO(b"audio bytes"))

            response = await service.analyze(upload_file=upload, time_size=10)

            self.assertEqual(len(response.result), 1)
            self.assertEqual(list(Path(tmp_dir).iterdir()), [])

    async def test_cleans_temp_files_when_decode_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = FailingDecodeAudioMetricsService(settings=_settings_for_tmp_dir(tmp_dir))
            upload = UploadFile(filename="sample.mp3", file=BytesIO(b"not audio"))

            with self.assertRaisesRegex(AudioMetricsProcessingError, "decode failed"):
                await service.analyze(upload_file=upload, time_size=10)

            self.assertEqual(list(Path(tmp_dir).iterdir()), [])

    async def test_cleans_temp_files_when_analysis_fails_after_decode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AnalyzerFailureAudioMetricsService(settings=_settings_for_tmp_dir(tmp_dir))
            upload = UploadFile(filename="sample.wav", file=BytesIO(b"audio bytes"))

            with self.assertRaisesRegex(AudioMetricsProcessingError, "analysis failed"):
                await service.analyze(upload_file=upload, time_size=10)

            self.assertEqual(list(Path(tmp_dir).iterdir()), [])


class FakeAudioMetricsService:
    def __init__(self) -> None:
        self.calls = 0

    async def analyze(self, *, upload_file: UploadFile, time_size: int) -> AudioMetricsResponse:
        self.calls += 1
        return AudioMetricsResponse(
            result=[
                AudioMetricItem(
                    bg=0.0,
                    ed=float(time_size),
                    max_db=-9.43,
                    db=-17.3,
                    min_db=-32.93,
                    snr=45.34,
                    max_volume=84,
                    volume=71,
                    min_volume=45,
                )
            ],
            task_id=f"task-287243d8_{upload_file.filename}",
            process_time_ms=12,
            timestamp=1748249265,
        )


class FailingAudioMetricsRouteService:
    async def analyze(self, *, upload_file: UploadFile, time_size: int) -> AudioMetricsResponse:
        raise AudioMetricsProcessingError("decode failed")


class AudioMetricsRouteTests(unittest.TestCase):
    def _client_with_service(self, service: object) -> TestClient:
        app = FastAPI()
        app.include_router(audio_metrics_router)
        app.dependency_overrides[get_audio_metrics_service] = lambda: service
        return TestClient(app)

    def test_audio_db_snr_route_returns_contract_response(self) -> None:
        service = FakeAudioMetricsService()
        client = self._client_with_service(service)

        response = client.post(
            "/audio/db_snr",
            data={"time_size": "10"},
            files={"audioFile": ("sample.wav", b"data", "audio/wav")},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(service.calls, 1)
        self.assertEqual(
            response.json(),
            {
                "result": [
                    {
                        "bg": 0.0,
                        "ed": 10.0,
                        "max_db": -9.43,
                        "db": -17.3,
                        "min_db": -32.93,
                        "snr": 45.34,
                        "max_volume": 84,
                        "volume": 71,
                        "min_volume": 45,
                    }
                ],
                "task_id": "task-287243d8_sample.wav",
                "process_time_ms": 12,
                "timestamp": 1748249265,
            },
        )

    def test_audio_db_snr_route_rejects_invalid_time_size_before_service(self) -> None:
        service = FakeAudioMetricsService()
        client = self._client_with_service(service)

        response = client.post(
            "/audio/db_snr",
            data={"time_size": "0"},
            files={"audioFile": ("sample.wav", b"data", "audio/wav")},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(service.calls, 0)

    def test_audio_db_snr_route_rejects_empty_filename_before_service(self) -> None:
        service = FakeAudioMetricsService()
        client = self._client_with_service(service)

        response = client.post(
            "/audio/db_snr",
            data={"time_size": "10"},
            files={"audioFile": ("", b"data", "audio/wav")},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(service.calls, 0)

    def test_audio_db_snr_route_maps_processing_errors(self) -> None:
        client = self._client_with_service(FailingAudioMetricsRouteService())

        with self.assertLogs("app.api.routes.audio_metrics", level="ERROR"):
            response = client.post(
                "/audio/db_snr",
                data={"time_size": "10"},
                files={"audioFile": ("sample.mp3", b"bad", "audio/mpeg")},
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "decode failed"})


if __name__ == "__main__":
    unittest.main()
