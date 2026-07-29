"""analyze_recording（SC-P1 録音/同期検証）の単体テスト。

純粋部（read_wav_facts / _resolve_manifest / _check）と analyze の合否判定を検証する。
実 WAV は wave 標準ライブラリでヘッダのみ有効なものを生成する（音声内容は無音・PII なし）。
実行: uv run pytest tools/verify/test_analyze_recording.py
"""

from __future__ import annotations

import json
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import analyze_recording as ar  # noqa: E402


def _write_wav(
    path: Path,
    *,
    sample_rate: int = 16_000,
    channels: int = 1,
    sample_width: int = 2,
    frames: int = 16_000,
) -> None:
    """指定フォーマットの無音 WAV を書く（ヘッダ検証用・内容は 0 埋め）。"""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sample_width)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00" * (frames * channels * sample_width))


def _session(tmp_path: Path, *, status: str = "complete", silence: float = 0.0) -> Path:
    """self/others の有効 WAV と manifest を持つ健全なセッションを作り manifest パスを返す。"""
    _write_wav(tmp_path / "self.wav", frames=16_000)  # 1.0s
    _write_wav(tmp_path / "others.wav", frames=16_000)  # 1.0s
    manifest = {
        "status": status,
        "commonStartUtc": "2026-07-28T00:00:00Z",
        "streams": [
            {"streamRole": "self", "wavPath": "self.wav", "durationSec": 1.0, "silenceFilledSec": silence},
            {"streamRole": "others", "wavPath": "others.wav", "durationSec": 1.0, "silenceFilledSec": silence},
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


# --- read_wav_facts --------------------------------------------------------
def test_read_wav_facts_valid(tmp_path: Path) -> None:
    path = tmp_path / "a.wav"
    _write_wav(path, frames=16_000)

    facts = ar.read_wav_facts(path)

    assert facts.valid
    assert (facts.sample_rate, facts.channels, facts.bit_depth) == (16_000, 1, 16)
    assert abs(facts.duration_sec - 1.0) < 1e-6


def test_read_wav_facts_missing(tmp_path: Path) -> None:
    facts = ar.read_wav_facts(tmp_path / "nope.wav")

    assert not facts.valid
    assert facts.error is not None and "存在しません" in facts.error


def test_read_wav_facts_empty(tmp_path: Path) -> None:
    path = tmp_path / "empty.wav"
    path.write_bytes(b"")

    facts = ar.read_wav_facts(path)

    assert not facts.valid
    assert facts.error is not None and "0 バイト" in facts.error


def test_read_wav_facts_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "bad.wav"
    path.write_bytes(b"this is not a RIFF/WAVE file")

    facts = ar.read_wav_facts(path)

    assert not facts.valid
    assert facts.error is not None


# --- _resolve_manifest / _check -------------------------------------------
def test_resolve_manifest_directory(tmp_path: Path) -> None:
    assert ar._resolve_manifest(tmp_path) == tmp_path / "manifest.json"


def test_resolve_manifest_file(tmp_path: Path) -> None:
    path = tmp_path / "m.json"
    path.write_text("{}", encoding="utf-8")
    assert ar._resolve_manifest(path) == path


def test_check_marks_by_state() -> None:
    assert ar._check("x", True, "d")[0] == "OK "
    assert ar._check("x", None, "d")[0] == "▲ "
    assert ar._check("x", False, "d")[0] == "NG "


# --- analyze（合否判定） ----------------------------------------------------
def test_analyze_pass_on_healthy_session(tmp_path: Path, capsys) -> None:
    rc = ar.analyze(_session(tmp_path))

    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out


def test_analyze_missing_manifest(tmp_path: Path, capsys) -> None:
    rc = ar.analyze(tmp_path / "manifest.json")

    assert rc == 1
    assert "manifest が見つかりません" in capsys.readouterr().out


def test_analyze_invalid_json(tmp_path: Path, capsys) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("{ broken", encoding="utf-8")

    rc = ar.analyze(path)

    assert rc == 1
    assert "解析失敗" in capsys.readouterr().out


def test_analyze_incomplete_status_fails(tmp_path: Path, capsys) -> None:
    rc = ar.analyze(_session(tmp_path, status="incomplete"))

    assert rc == 1
    assert "FAIL" in capsys.readouterr().out


def test_analyze_wrong_wav_format_fails(tmp_path: Path, capsys) -> None:
    # 8kHz の WAV は 16k/mono/16bit 要件に反するため NG → FAIL。
    _write_wav(tmp_path / "self.wav", sample_rate=8_000, frames=8_000)
    _write_wav(tmp_path / "others.wav", frames=16_000)
    manifest = {
        "status": "complete",
        "commonStartUtc": "2026-07-28T00:00:00Z",
        "streams": [
            {"streamRole": "self", "wavPath": "self.wav", "durationSec": 1.0, "silenceFilledSec": 0.0},
            {"streamRole": "others", "wavPath": "others.wav", "durationSec": 1.0, "silenceFilledSec": 0.0},
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    rc = ar.analyze(path)

    assert rc == 1


def test_analyze_high_silence_ratio_fails(tmp_path: Path, capsys) -> None:
    # 無音補填が閾値(5%)超 → 同期NG → FAIL。
    rc = ar.analyze(_session(tmp_path, silence=0.5))

    assert rc == 1
    assert "FAIL" in capsys.readouterr().out


# --- main ------------------------------------------------------------------
def test_main_requires_single_arg(capsys) -> None:
    assert ar.main([]) == 2


def test_main_runs_analyze(tmp_path: Path) -> None:
    assert ar.main([str(_session(tmp_path))]) == 0
