"""入力解決（L1 / BR-IN）。

paired（Unit A の sessionDir/manifest.json）または single（単一WAV検証, Q7=A）の入力を
RecordingInput に解決する。WAV は 16kHz/mono/16bit PCM 前提（BR-IN-04）。Unit A の正規化
責務を侵さない（再正規化はしない＝責務分離）。連携点は WAV と manifest のみ（BR-IN-05）。
"""

from __future__ import annotations

import json
import wave
from pathlib import Path

from .errors import PipelineError
from .models import InputMode, RecordingInput, RecordingManifest, StreamRole

_STAGE = "input"

# Unit A 正規化フォーマット（FR-05）。
_EXPECTED_SAMPLE_RATE = 16_000
_EXPECTED_CHANNELS = 1
_EXPECTED_SAMPLE_WIDTH_BYTES = 2  # 16bit


def resolve_paired(session_dir: Path, language: str) -> RecordingInput:
    """paired 本番入力を manifest.json から解決する（BR-IN-01）。"""
    manifest_path = session_dir / "manifest.json"
    if not manifest_path.is_file():
        raise PipelineError(f"manifest.json が見つかりません: {manifest_path}", failed_stage=_STAGE)

    manifest = _read_manifest(manifest_path)
    self_meta = manifest.stream(StreamRole.SELF)
    others_meta = manifest.stream(StreamRole.OTHERS)
    if self_meta is None or others_meta is None:
        raise PipelineError(
            "manifest に self / others 両系統が揃っていません（Unit A の BR-IO-03）。",
            failed_stage=_STAGE,
        )

    self_wav = _resolve_wav(session_dir, self_meta.wav_path)
    others_wav = _resolve_wav(session_dir, others_meta.wav_path)
    # 連携契約のメタを信頼しつつ、実体 WAV のフォーマットも検証する（BR-IN-04）。
    _validate_wav_format(self_wav)
    _validate_wav_format(others_wav)

    return RecordingInput(
        mode=InputMode.PAIRED,
        session_id=manifest.session_id,
        others_wav_path=others_wav,
        self_wav_path=self_wav,
        language=language,
        common_start_utc=manifest.common_start_utc,
        is_partial=manifest.is_incomplete,  # BR-IN-03（停止せず注記）
    )


def resolve_single(wav_path: Path, language: str) -> RecordingInput:
    """single 検証入力を解決する（Q7=A / BR-IN-02。others 系統のみ・絶対時刻統合なし）。"""
    if not wav_path.is_file():
        raise PipelineError(f"WAV が見つかりません: {wav_path}", failed_stage=_STAGE)
    _validate_wav_format(wav_path)

    return RecordingInput(
        mode=InputMode.SINGLE,
        session_id=wav_path.stem,
        others_wav_path=wav_path,
        self_wav_path=None,
        language=language,
        common_start_utc=None,
        is_partial=False,
    )


def _read_manifest(path: Path) -> RecordingManifest:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"manifest.json の読込に失敗しました: {path}", failed_stage=_STAGE) from exc
    try:
        return RecordingManifest.from_json(data)
    except (KeyError, ValueError) as exc:
        raise PipelineError(f"manifest.json の形式が不正です: {exc}", failed_stage=_STAGE) from exc


def _resolve_wav(session_dir: Path, wav_path: str) -> Path:
    """manifest の wavPath を解決する。相対パスは session_dir 起点で扱う。"""
    candidate = Path(wav_path)
    if not candidate.is_absolute():
        # まず session_dir 直下、無ければ記録されたパスのファイル名で補完する。
        local = session_dir / candidate.name
        candidate = local if local.is_file() else session_dir / candidate
    if not candidate.is_file():
        raise PipelineError(f"WAV が見つかりません: {candidate}", failed_stage=_STAGE)
    return candidate


def _validate_wav_format(path: Path) -> None:
    """16kHz/mono/16bit PCM を検証する（BR-IN-04）。不一致は再正規化せずエラー。"""
    try:
        with wave.open(str(path), "rb") as wav:
            sample_rate = wav.getframerate()
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
    except (OSError, wave.Error) as exc:
        raise PipelineError(f"WAV を開けません: {path}", failed_stage=_STAGE) from exc

    if (
        sample_rate != _EXPECTED_SAMPLE_RATE
        or channels != _EXPECTED_CHANNELS
        or sample_width != _EXPECTED_SAMPLE_WIDTH_BYTES
    ):
        raise PipelineError(
            "WAV フォーマットが Unit A 正規化前提(16kHz/mono/16bit)と一致しません: "
            f"{path.name} = {sample_rate}Hz/{channels}ch/{sample_width * 8}bit",
            failed_stage=_STAGE,
        )
