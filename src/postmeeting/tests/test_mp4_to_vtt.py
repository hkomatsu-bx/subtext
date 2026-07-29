"""mp4→VTT オーケストレーションのテスト（AWS は fake 注入で代替）。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from subtext_postmeeting.config import PipelineConfig
from subtext_postmeeting.errors import PipelineError
from subtext_postmeeting import mp4_to_vtt
from subtext_postmeeting.mp4_to_vtt import _safe_id, convert, extract_audio

from .conftest import others_transcribe_json


def _fake_extract(mp4_path: Path, out_path: Path) -> None:
    """ffmpeg の代わりにダミー音声ファイルを書く（抽出された体）。"""
    out_path.write_bytes(b"fLaC\x00\x00\x00\x22")


class _FakeS3:
    """S3Io のスタブ。アップロード/取得/削除を記録する。"""

    def __init__(self) -> None:
        self.bucket = "test-bucket"
        self.uploaded: list[tuple[str, str]] = []
        self.deleted_prefixes: list[str] = []
        self.verified = False

    def verify_preconditions(self) -> None:
        self.verified = True

    def upload(self, local_path: Path, key: str) -> str:
        self.uploaded.append((str(local_path), key))
        return f"s3://{self.bucket}/{key}"

    def download_json(self, key: str) -> dict[str, Any]:
        return others_transcribe_json()

    def delete_prefix(self, prefix: str) -> int:
        self.deleted_prefixes.append(prefix)
        return 1


class _FakeTranscribe:
    def __init__(self) -> None:
        self.spec: Any = None

    def run_job(self, spec: Any, output_bucket: str, output_key: str, poll_timeout_sec: int) -> str:
        self.spec = spec
        return output_key


def _config(tmp_path: Path) -> PipelineConfig:
    return PipelineConfig(
        aws_region="ap-northeast-1",
        s3_bucket="test-bucket",
        s3_prefix="subtext/jobs/",
        language="ja-JP",
        max_speakers=5,
        poll_timeout_sec=10,
        keep_s3=False,
        output_dir=tmp_path,
        bedrock_model_id="",
        vocabulary_name="",
        correction_terms_path=tmp_path / "no-terms.json",  # 非存在→補正 no-op
    )


def _mp4(tmp_path: Path, name: str = "meeting.mp4") -> Path:
    p = tmp_path / name
    p.write_bytes(b"\x00\x00\x00\x18ftypmp42")  # 中身は使わない（Transcribe は fake）。
    return p


def test_convert_extracts_audio_then_uses_flac_media_format(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    s3, tc = _FakeS3(), _FakeTranscribe()
    out = tmp_path / "会議.vtt"

    result = convert(
        _mp4(tmp_path),
        out,
        cfg,
        s3io=s3,
        transcribe=tc,
        credential_checker=lambda: None,
        audio_extractor=_fake_extract,
    )

    assert result == out
    text = out.read_text(encoding="utf-8")
    assert text.startswith("WEBVTT")
    assert "<v spk_0>" in text and "<v spk_1>" in text  # conftest は話者2名
    # 音声抽出後の FLAC を Transcribe にかける（話者分離 on）こと、FLAC をアップロードすること。
    assert tc.spec.media_format == "flac"
    assert tc.spec.show_speaker_labels is True
    assert s3.uploaded[0][1].endswith("audio.flac")
    assert s3.verified is True


def test_convert_cleans_up_s3_and_tempfiles(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    s3 = _FakeS3()
    captured: dict[str, Path] = {}

    def _extract(mp4_path: Path, out_path: Path) -> None:
        captured["audio"] = out_path
        out_path.write_bytes(b"fLaC")

    convert(
        _mp4(tmp_path),
        tmp_path / "o.vtt",
        cfg,
        s3io=s3,
        transcribe=_FakeTranscribe(),
        credential_checker=lambda: None,
        audio_extractor=_extract,
    )
    assert s3.deleted_prefixes == ["subtext/jobs/meeting/"]
    assert not captured["audio"].exists()  # 抽出音声(機微)は破棄される


def test_convert_keeps_s3_when_requested(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    s3 = _FakeS3()
    convert(
        _mp4(tmp_path),
        tmp_path / "o.vtt",
        cfg,
        keep_s3=True,
        s3io=s3,
        transcribe=_FakeTranscribe(),
        credential_checker=lambda: None,
        audio_extractor=_fake_extract,
    )
    assert s3.deleted_prefixes == []


def test_convert_refuses_to_overwrite_existing_vtt(tmp_path: Path) -> None:
    """既存の出力 VTT は上書きせず、課金段へ入る前に停止すること。

    Teams は録画 mp4 と実名入り VTT を同じフォルダへ出す（本プロジェクトが対応する2入力そのもの）。
    上書きすると実名入りトランスクリプト＝より良い成果物が失われ、しかも Transcribe を満額
    再課金する。録音側の上書き禁止（SyncRecorder / BR-IO-01）と同じ扱いにする。
    """
    cfg = _config(tmp_path)
    out = tmp_path / "meeting.vtt"
    out.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n<v 田中太郎>こんにちは\n", encoding="utf-8")
    s3, tc = _FakeS3(), _FakeTranscribe()

    with pytest.raises(PipelineError) as ei:
        convert(
            _mp4(tmp_path),
            out,
            cfg,
            s3io=s3,
            transcribe=tc,
            credential_checker=lambda: None,
            audio_extractor=_fake_extract,
        )

    assert "既に存在します" in str(ei.value)
    assert "田中太郎" in out.read_text(encoding="utf-8"), "既存 VTT が保全されること"
    assert s3.uploaded == [] and s3.verified is False, "課金段（S3/Transcribe）へ入らないこと"
    assert tc.spec is None


def test_convert_force_overwrites_existing_vtt(tmp_path: Path) -> None:
    """--force は意図した再変換の逃げ道として上書きを許すこと。"""
    cfg = _config(tmp_path)
    out = tmp_path / "meeting.vtt"
    out.write_text("古い VTT", encoding="utf-8")

    convert(
        _mp4(tmp_path),
        out,
        cfg,
        force=True,
        s3io=_FakeS3(),
        transcribe=_FakeTranscribe(),
        credential_checker=lambda: None,
        audio_extractor=_fake_extract,
    )

    assert out.read_text(encoding="utf-8").startswith("WEBVTT")


def test_safe_id_strips_non_ascii_for_transcribe_job_name() -> None:
    # Transcribe の TranscriptionJobName は ASCII（[0-9a-zA-Z._-]）限定。日本語・括弧・空白を畳む。
    sid = _safe_id("【サンプルJP様】定例会-20260624_015702UTC-Meeting Recording")
    assert re.fullmatch(r"[0-9A-Za-z._-]+", sid)  # 非 ASCII が残らない
    assert "JP" in sid and "20260624_015702UTC" in sid
    assert "--" not in sid  # 連続ダッシュは圧縮


def test_safe_id_falls_back_when_all_stripped() -> None:
    assert _safe_id("会議録") == "session"


def test_missing_mp4_raises(tmp_path: Path) -> None:
    with pytest.raises(PipelineError):
        convert(
            tmp_path / "nope.mp4",
            tmp_path / "o.vtt",
            _config(tmp_path),
            s3io=_FakeS3(),
            transcribe=_FakeTranscribe(),
            credential_checker=lambda: None,
            audio_extractor=_fake_extract,
        )


# --- extract_audio（ffmpeg）---------------------------------------------------
def test_extract_audio_missing_ffmpeg_is_actionable(tmp_path: Path, monkeypatch: Any) -> None:
    def _raise(*_a: Any, **_k: Any) -> Any:
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(mp4_to_vtt.subprocess, "run", _raise)
    with pytest.raises(PipelineError) as ei:
        extract_audio(tmp_path / "in.mp4", tmp_path / "out.flac")
    assert "ffmpeg" in str(ei.value)


def test_extract_audio_nonzero_returncode_raises(tmp_path: Path, monkeypatch: Any) -> None:
    class _Proc:
        returncode = 1
        stderr = b"Invalid data found when processing input"

    monkeypatch.setattr(mp4_to_vtt.subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(PipelineError) as ei:
        extract_audio(tmp_path / "in.mp4", tmp_path / "out.flac")
    assert "returncode=1" in str(ei.value)
    # stderr（顧客名を含みうるパス）はメッセージに出さない（BR-ERR-04）。
    assert "Invalid data" not in str(ei.value)


def test_extract_audio_no_audio_stream_is_actionable(tmp_path: Path, monkeypatch: Any) -> None:
    # 映像のみ mp4: -vn 後にストリームが消え ffmpeg が "does not contain any stream" で落ちる。
    class _Proc:
        returncode = 4294967274
        stderr = b"Output #0, flac\nOutput file does not contain any stream\nInvalid argument"

    monkeypatch.setattr(mp4_to_vtt.subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(PipelineError) as ei:
        extract_audio(tmp_path / "in.mp4", tmp_path / "out.flac")
    assert "音声トラックがありません" in str(ei.value)
