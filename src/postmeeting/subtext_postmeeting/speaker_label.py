"""実名割当（L6 / BR-NAME・FR-09）。

ファイル方式（Q3=A・非対話・再現性重視）。マッピングファイル未存在ならテンプレ（spk_n＋代表
発言サンプル, self="自分"）を生成し merged 段で停止する（BR-NAME-01）。存在すれば適用し、未記入
ラベルは元のラベルのまま維持して unresolved に列挙する（BR-NAME-02）。
実名(PII)はログ出力せず S3 へ送らない（BR-NAME-04）— 本モジュールはローカルファイルのみ扱う。
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .errors import PipelineError
from .models import (
    FinalTranscript,
    ResolvedSegment,
    SpeakerCluster,
    SpeakerNameMap,
    StreamRole,
)

_STAGE = "name"
_SELF_DEFAULT_NAME = "自分"
_SAMPLE_UTTERANCES = 3  # 代表発言の提示件数
# 自動補完（speakerNameHints 由来）のラベルを記録するキー。`_clusters` と同じアンダースコア前置の
# 補助情報で、Unit B は読み戻さない（SpeakerNameMap.from_json は未知キーを無視する）。
# 目的は「人が確認した実名」と「機械が入れた候補」の区別（BR-NAME-01。下流の命名ゲートが参照する）。
_HINTED_LABELS_KEY = "_hintedLabels"


# ---------------------------------------------------------------------------
# 純粋関数
# ---------------------------------------------------------------------------
def build_clusters(transcript: FinalTranscript) -> list[SpeakerCluster]:
    """others 系統の話者ごとにクラスタ（代表発言・件数・総秒）を集計する（純粋）。"""
    by_label: dict[str, list[ResolvedSegment]] = defaultdict(list)
    for seg in transcript.segments:
        if seg.origin == StreamRole.OTHERS:
            by_label[seg.speaker].append(seg)

    return [_build_cluster(label, by_label[label]) for label in sorted(by_label)]


def _build_cluster(label: str, segs: list[ResolvedSegment]) -> SpeakerCluster:
    """1話者ラベルのセグメント群を代表発言・件数・総秒へ集計する（純粋）。"""
    samples = tuple(s.text for s in segs[:_SAMPLE_UTTERANCES] if s.text)
    total_sec = sum(max(0.0, s.end_sec - s.start_sec) for s in segs)
    return SpeakerCluster(
        label=label,
        sample_utterances=samples,
        segment_count=len(segs),
        total_sec=total_sec,
    )


def apply_mapping(transcript: FinalTranscript, name_map: SpeakerNameMap) -> FinalTranscript:
    """マッピングを適用した新しい FinalTranscript を返す（純粋・非破壊, BR-NAME-02）。

    未記入（空文字マッピング／欠落）の others 話者ラベルは元のラベルのまま維持し、`unresolved`
    として `model_info["naming"]` に列挙する。これにより下流（議事録生成・確認）が
    実名未解決の話者を認識できる。列挙するのはラベル（spk_n）のみで PII を含めない（BR-NAME-04）。
    """
    mappings = {k: v for k, v in name_map.mappings.items() if v.strip()}
    new_segments = tuple(_rename(seg, mappings.get(seg.speaker, seg.speaker)) for seg in transcript.segments)

    unresolved = sorted(
        {seg.speaker for seg in transcript.segments if seg.origin == StreamRole.OTHERS and seg.speaker not in mappings}
    )
    speakers = list(dict.fromkeys(seg.speaker for seg in new_segments))  # 出現順を保った重複排除
    model_info = dict(transcript.model_info)
    model_info["naming"] = {"unresolved": unresolved}
    return FinalTranscript(
        session_id=transcript.session_id,
        language=transcript.language,
        segments=new_segments,
        speakers=tuple(speakers),
        common_start_utc=transcript.common_start_utc,
        model_info=model_info,
        is_partial=transcript.is_partial,
    )


def build_template(transcript: FinalTranscript) -> dict[str, Any]:
    """マッピングファイルのテンプレ内容を組み立てる（純粋）。

    model_info["speakerNameHints"]（VTT 経路が話者名を供給）があればテンプレ値の初期値に
    入れる。Transcribe 経路はヒントを持たないため従来どおり空文字（記入待ち）になる。

    自動補完したラベルは `_hintedLabels` に記録する。ヒントは実名とは限らず、ライブ字幕由来の
    「自分」「相手」や Teams の「Speaker 1」のような仮名になり得る。値が入っていることを
    「人が確認済み」と解釈すると命名ゲート（BR-NAME-01）が素通りし、参加者全員が仮名 1 人に
    統合された議事録が出来てしまうため、確認済みかどうかを値の有無と別に持つ。
    """
    clusters = build_clusters(transcript)
    hints = transcript.model_info.get("speakerNameHints", {})
    mappings: dict[str, str] = {c.label: str(hints.get(c.label, "")) for c in clusters}
    hinted = sorted(label for label, name in mappings.items() if name.strip())
    mappings["self"] = _SELF_DEFAULT_NAME
    template: dict[str, Any] = {
        "sessionId": transcript.session_id,
        "mappings": mappings,
        "unresolved": [],
        "_clusters": [c.to_json() for c in clusters],  # 参考: 誰か判別の手がかり
    }
    if hinted:
        template[_HINTED_LABELS_KEY] = hinted
    return template


# ---------------------------------------------------------------------------
# ファイル IO
# ---------------------------------------------------------------------------
def ensure_naming(transcript: FinalTranscript, naming_path: Path) -> SpeakerNameMap | None:
    """マッピングファイルを解決する。

    未存在ならテンプレを生成して None を返す（呼び出し側は merged 段で停止）。存在すれば
    読み込んで SpeakerNameMap を返す（BR-NAME-01/02）。
    """
    if not naming_path.is_file():
        _write_template(transcript, naming_path)
        return None
    return _read_mapping(naming_path)


def _write_template(transcript: FinalTranscript, naming_path: Path) -> None:
    naming_path.parent.mkdir(parents=True, exist_ok=True)
    template = build_template(transcript)
    try:
        naming_path.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        raise PipelineError(f"話者マッピングテンプレの書込に失敗しました: {naming_path}", failed_stage=_STAGE) from exc


def _read_mapping(naming_path: Path) -> SpeakerNameMap:
    try:
        data = json.loads(naming_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"話者マッピングファイルの読込に失敗しました: {naming_path}", failed_stage=_STAGE) from exc
    return SpeakerNameMap.from_json(data)


def _rename(seg: ResolvedSegment, new_speaker: str) -> ResolvedSegment:
    if new_speaker == seg.speaker:
        return seg
    return ResolvedSegment(
        speaker=new_speaker,
        origin=seg.origin,
        start_sec=seg.start_sec,
        end_sec=seg.end_sec,
        text=seg.text,
        confidence=seg.confidence,
        absolute_start_utc=seg.absolute_start_utc,
    )
