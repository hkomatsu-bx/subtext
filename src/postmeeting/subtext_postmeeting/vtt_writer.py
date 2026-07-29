"""WebVTT 出力（mp4→VTT ツール用）【純粋関数】。

Transcribe 結果のパース済み RawTranscript を WebVTT に変換する。話者は `<v spk_0>` の
ボイスタグで表現し、Teams 等が出力する VTT と同じ書式に揃える（後段の vtt_parser がそのまま
読める）。実名は持たないため命名は後段（`--mode vtt` の命名ゲート）で付与する。
本文の `&`/`<`/`>` は WebVTT 仕様に従いエスケープする。音声内容はログに出さない（BR-ERR-04）。
"""

from __future__ import annotations

from .models import RawTranscript


def to_vtt(transcript: RawTranscript) -> str:
    """RawTranscript を WebVTT 文字列に変換する（純粋）。"""
    lines: list[str] = ["WEBVTT", ""]
    for index, seg in enumerate(transcript.segments, start=1):
        lines.append(str(index))
        lines.append(f"{_fmt_timestamp(seg.start_sec)} --> {_fmt_timestamp(seg.end_sec)}")
        lines.append(f"<v {seg.speaker_label}>{_escape(seg.text)}</v>")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _fmt_timestamp(seconds: float) -> str:
    """秒を WebVTT の `HH:MM:SS.mmm` に整形する。"""
    total_ms = int(round(max(0.0, seconds) * 1000.0))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def _escape(text: str) -> str:
    """WebVTT キュー本文の特殊文字をエスケープする（順序重要: & を最初に）。"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
