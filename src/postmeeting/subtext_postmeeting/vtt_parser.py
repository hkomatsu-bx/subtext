"""VTT 入力パース（VTT 経路 / FR-16 入口拡張）【純粋関数】。

Teams が出力する WebVTT（`<v 話者名>` ボイスタグ＋`HH:MM:SS.mmm` キュー）を受け取り、
Amazon Transcribe を経由せずに FinalTranscript を直接生成する。話者は出現順に spk_0.. の
安定ラベルを割り当て、VTT の話者名は model_info["speakerNameHints"] に保持して命名ゲートの
初期値に供する（実名割当・補正は別段で人手対応, BR-NAME-01）。VTT には実名(PII)が直書き
されるため、本モジュールは音声内容・実名をログに出さない（BR-ERR-04, NFR-SEC-04）。

連携契約は VTT ファイルと FinalTranscript のみ（FR-16）。Transcribe 経路の parser.py とは別系統
だが、出力型（FinalTranscript / ResolvedSegment）を共有して下流（命名・要約）と疎結合に接続する。
"""

from __future__ import annotations

import html
import re

from .errors import PipelineError
from .models import FinalTranscript, ResolvedSegment, StreamRole

_STAGE = "input"
_SOURCE = "vtt"

# 「HH:MM:SS.mmm --> HH:MM:SS.mmm（任意の cue 設定が後続しうる）」。MM:SS.mmm も許容。
_TIMING_RE = re.compile(
    r"(?P<start>(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*"
    r"(?P<end>(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})"
)
# ボイスタグ <v 話者名> または <v.class 話者名>。名前はタグ内の残り全体。
_VOICE_RE = re.compile(r"<v(?:\.[^\s>]+)?\s+([^>]*)>", re.IGNORECASE)
# 残る全タグ（</v>, <c>, インライン <00:00:01.000> 等）を本文から除去する。
_TAG_RE = re.compile(r"<[^>]*>")
# 機械話者ラベル（spk_0 等）。mp4→VTT ツール出力の VTT はこの形のため、実名ヒントに
# は採用しない（命名ゲートのテンプレ初期値を空＝記入待ちに保ち、Transcribe 経路と揃える）。
_MACHINE_LABEL_RE = re.compile(r"^spk_\d+$", re.IGNORECASE)


def parse_vtt(vtt_text: str, session_id: str, language: str) -> FinalTranscript:
    """WebVTT テキストを FinalTranscript（実名適用前・merged 相当）に変換する。"""
    cues = _read_cues(vtt_text)
    if not cues:
        raise PipelineError("VTT に有効なキュー（タイムスタンプ＋本文）が見つかりません。", failed_stage=_STAGE)

    label_by_key: dict[str, str] = {}
    hints: dict[str, str] = {}
    segments: list[ResolvedSegment] = []
    next_index = 0

    for start_sec, end_sec, name, text in cues:
        key = name if name else ""  # 無名キューは単一ラベルに集約する。
        label = label_by_key.get(key)
        if label is None:
            label = f"spk_{next_index}"
            label_by_key[key] = label
            _maybe_add_hint(hints, label, name)  # 実名のみヒント化（機械ラベルは記入待ち）
            next_index += 1
        segments.append(
            ResolvedSegment(
                speaker=label,
                origin=StreamRole.OTHERS,  # VTT は全話者を相手系統として扱う（self なし）。
                start_sec=start_sec,
                end_sec=end_sec,
                text=text,
                confidence=None,  # VTT は信頼度を持たない。
                absolute_start_utc=None,  # 絶対時刻アンカーなし（single 相当）。
            )
        )

    speakers = tuple(dict.fromkeys(seg.speaker for seg in segments))
    model_info: dict[str, object] = {"source": _SOURCE}
    if hints:
        model_info["speakerNameHints"] = hints

    return FinalTranscript(
        session_id=session_id,
        language=language,
        segments=tuple(segments),
        speakers=speakers,
        common_start_utc=None,
        model_info=model_info,
        is_partial=False,
    )


def _maybe_add_hint(hints: dict[str, str], label: str, name: str | None) -> None:
    """実名の話者名のみを命名ゲートのヒントに登録する（機械ラベル spk_N は記入待ちに保つ）。"""
    if name and not _MACHINE_LABEL_RE.match(name):
        hints[label] = name


def _read_cues(vtt_text: str) -> list[tuple[float, float, str | None, str]]:
    """ブロック単位で（開始秒, 終了秒, 話者名|None, 本文）のキュー列を抽出する（純粋）。"""
    text = vtt_text.lstrip("﻿")  # 先頭 BOM を除去。
    cues: list[tuple[float, float, str | None, str]] = []
    for block in re.split(r"\r?\n\r?\n", text.strip()):
        lines = block.splitlines()
        timing_idx = next((i for i, line in enumerate(lines) if _TIMING_RE.search(line)), None)
        if timing_idx is None:
            continue  # WEBVTT ヘッダ・NOTE・STYLE 等のタイミングなしブロックは飛ばす。
        match = _TIMING_RE.search(lines[timing_idx])
        assert match is not None  # timing_idx の条件で保証される。
        payload = "\n".join(lines[timing_idx + 1 :]).strip()
        if not payload:
            continue
        voice = _VOICE_RE.search(payload)
        name = _unescape(voice.group(1).strip()) if voice else None
        body = _unescape(" ".join(_TAG_RE.sub("", payload).split()))  # タグ除去＋空白正規化→実体参照復元。
        if not body:
            continue
        cues.append((_to_seconds(match.group("start")), _to_seconds(match.group("end")), name, body))
    return cues


def _unescape(text: str) -> str:
    """WebVTT の文字実体参照を元の文字へ戻す（`&amp;`→`&`, `&lt;`→`<` 等・純粋）。

    WebVTT は本文・話者名の `&` と `<` をエスケープする仕様で、`vtt_writer._escape` も同じ変換を
    掛ける。復元しないと `A&B` が `A&amp;B` のまま final_transcript.json → 議事録 → Slack まで
    流れ、話者名も `Smith &amp; Sons` のように恒久的に壊れる（命名ゲートの初期値も同様）。

    **タグ除去より後に呼ぶこと**: 先に復元すると本文の `&lt;` が `<` になり、その先が
    タグとみなされて本文の一部が消える。
    """
    return html.unescape(text)


def _to_seconds(timestamp: str) -> float:
    """`HH:MM:SS.mmm` / `MM:SS.mmm`（`,` ミリ秒区切りも可）を秒に変換する（純粋）。"""
    parts = [float(p) for p in timestamp.replace(",", ".").split(":")]
    if len(parts) == 3:
        hours, minutes, seconds = parts
    elif len(parts) == 2:
        hours, minutes, seconds = 0.0, parts[0], parts[1]
    else:
        return parts[0]
    return hours * 3600.0 + minutes * 60.0 + seconds
