"""テスト共通フィクスチャ・ヘルパ。"""

from __future__ import annotations

import wave
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def make_wav(tmp_path: Path):
    """16kHz/mono/16bit PCM の有効な WAV を生成するファクトリ（BR-IN-04 前提）。"""

    def _make(
        name: str = "audio.wav",
        *,
        sample_rate: int = 16_000,
        channels: int = 1,
        sample_width: int = 2,
        frames: int = 1600,
    ) -> Path:
        path = tmp_path / name
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(channels)
            wav.setsampwidth(sample_width)
            wav.setframerate(sample_rate)
            wav.writeframes(b"\x00\x00" * frames)
        return path

    return _make


def others_transcribe_json(job_name: str = "job-others") -> dict[str, Any]:
    """話者2名の Transcribe(others) 出力のミニ表現。"""
    return {
        "jobName": job_name,
        "results": {
            "items": [
                _pron("0.0", "0.5", "0.9", "おはよう"),
                _punc("。"),
                _pron("3.0", "3.5", "0.8", "こんにちは"),
                _pron("3.5", "4.0", "0.7", "ございます"),
            ],
            "speaker_labels": {
                "speakers": 2,
                "segments": [
                    _spk_seg("0.0", "0.5", "spk_0", ["0.0"]),
                    _spk_seg("3.0", "4.0", "spk_1", ["3.0", "3.5"]),
                ],
            },
        },
    }


def self_transcribe_json(job_name: str = "job-self") -> dict[str, Any]:
    """話者分離なし（self）の Transcribe 出力のミニ表現。休止で2セグメントに割れる。"""
    return {
        "jobName": job_name,
        "results": {
            "items": [
                _pron("0.0", "0.5", "0.9", "はい"),
                _pron("0.6", "1.0", "0.8", "そうです"),
                _punc("。"),
                _pron("5.0", "5.5", "0.7", "では"),
            ]
        },
    }


def _pron(start: str, end: str, conf: str, content: str) -> dict[str, Any]:
    return {
        "type": "pronunciation",
        "start_time": start,
        "end_time": end,
        "alternatives": [{"confidence": conf, "content": content}],
    }


def _punc(content: str) -> dict[str, Any]:
    return {"type": "punctuation", "alternatives": [{"confidence": "0.0", "content": content}]}


def _spk_seg(start: str, end: str, label: str, item_starts: list[str]) -> dict[str, Any]:
    return {
        "start_time": start,
        "end_time": end,
        "speaker_label": label,
        "items": [{"start_time": s, "speaker_label": label} for s in item_starts],
    }
