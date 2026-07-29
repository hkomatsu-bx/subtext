"""入力解決のテスト（BR-IN）。paired/single/partial/フォーマット検証を確認する。"""

from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest

from subtext_postmeeting.errors import PipelineError
from subtext_postmeeting.input_resolver import resolve_paired, resolve_single
from subtext_postmeeting.models import InputMode


def _write_wav(path: Path, *, sample_rate: int = 16_000, channels: int = 1, width: int = 2) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00\x00" * 1600)


def _write_manifest(session_dir: Path, status: str = "complete") -> None:
    _write_wav(session_dir / "self.wav")
    _write_wav(session_dir / "others.wav")
    manifest = {
        "sessionId": "20260620-100000",
        "createdAtUtc": "2026-06-20T01:00:05Z",
        "commonStartUtc": "2026-06-20T01:00:00Z",
        "status": status,
        "streams": [
            {
                "wavPath": "self.wav",
                "streamRole": "self",
                "startTimeUtc": "2026-06-20T01:00:00Z",
                "sampleRate": 16000,
                "channels": 1,
                "bitDepth": 16,
                "deviceName": "mic",
                "durationSec": 0.1,
                "silenceFilledSec": 0.0,
            },
            {
                "wavPath": "others.wav",
                "streamRole": "others",
                "startTimeUtc": "2026-06-20T01:00:00Z",
                "sampleRate": 16000,
                "channels": 1,
                "bitDepth": 16,
                "deviceName": "loopback",
                "durationSec": 0.1,
                "silenceFilledSec": 0.0,
            },
        ],
    }
    (session_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


class TestResolveSingle:
    def test_resolves_single_from_valid_wav(self, tmp_path: Path) -> None:
        wav = tmp_path / "meeting.wav"
        _write_wav(wav)
        rec = resolve_single(wav, "ja-JP")
        assert rec.mode == InputMode.SINGLE
        assert rec.session_id == "meeting"
        assert rec.self_wav_path is None
        assert rec.common_start_utc is None

    def test_rejects_wrong_format(self, tmp_path: Path) -> None:
        wav = tmp_path / "bad.wav"
        _write_wav(wav, sample_rate=44_100)  # 16kHz でない（BR-IN-04）
        with pytest.raises(PipelineError):
            resolve_single(wav, "ja-JP")

    def test_missing_file_errors(self, tmp_path: Path) -> None:
        with pytest.raises(PipelineError):
            resolve_single(tmp_path / "nope.wav", "ja-JP")


class TestResolvePaired:
    def test_resolves_paired_from_manifest(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path)
        rec = resolve_paired(tmp_path, "ja-JP")
        assert rec.mode == InputMode.PAIRED
        assert rec.session_id == "20260620-100000"
        assert rec.self_wav_path is not None
        assert rec.common_start_utc is not None
        assert rec.is_partial is False

    def test_incomplete_manifest_marks_partial(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, status="incomplete")
        rec = resolve_paired(tmp_path, "ja-JP")
        assert rec.is_partial is True  # BR-IN-03（停止せず注記）

    def test_missing_manifest_errors(self, tmp_path: Path) -> None:
        with pytest.raises(PipelineError):
            resolve_paired(tmp_path, "ja-JP")
