"""統合（L5 / BR-MERGE）【純粋関数】。

self/others のセグメントを録音開始(0)共通基準の startSec 昇順でマージし FinalTranscript を返す
（BR-MERGE-01）。paired かつ commonStartUtc 有りで absoluteStartUtc を付与（BR-MERGE-02）。
外部状態を読まない純粋変換で、同入力→同出力（BR-MERGE-03, FR-11）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .models import (
    FinalTranscript,
    RawTranscript,
    ResolvedSegment,
    StreamRole,
    TranscriptSegment,
)


def merge(
    raw_others: RawTranscript,
    raw_self: RawTranscript | None,
    common_start_utc: datetime | None,
    *,
    is_partial: bool = False,
) -> FinalTranscript:
    """others（必須）と self（paired のみ）を統合して FinalTranscript を構築する。"""
    resolved: list[ResolvedSegment] = []
    resolved.extend(_to_resolved(raw_others, StreamRole.OTHERS, common_start_utc))
    if raw_self is not None:
        resolved.extend(_to_resolved(raw_self, StreamRole.SELF, common_start_utc))

    # startSec 昇順で安定整列（両系統とも録音開始0基準）。同時刻は others を先に置く。
    resolved.sort(key=lambda s: (s.start_sec, 0 if s.origin == StreamRole.OTHERS else 1))

    speakers = list(dict.fromkeys(s.speaker for s in resolved))  # 出現順を保った重複排除
    model_info = {
        "others": raw_others.model_info,
        **({"self": raw_self.model_info} if raw_self is not None else {}),
    }
    return FinalTranscript(
        session_id=raw_others.session_id,
        language=raw_others.language,
        segments=tuple(resolved),
        speakers=tuple(speakers),
        common_start_utc=common_start_utc,
        model_info=model_info,
        is_partial=is_partial,
    )


def _to_resolved(raw: RawTranscript, origin: StreamRole, common_start_utc: datetime | None) -> list[ResolvedSegment]:
    return [_resolve_segment(seg, origin, common_start_utc) for seg in raw.segments]


def _resolve_segment(seg: TranscriptSegment, origin: StreamRole, common_start_utc: datetime | None) -> ResolvedSegment:
    """1 セグメントを ResolvedSegment へ変換する。commonStartUtc 有りで絶対時刻を付与（BR-MERGE-02）。"""
    absolute = common_start_utc + timedelta(seconds=seg.start_sec) if common_start_utc is not None else None
    return ResolvedSegment(
        speaker=seg.speaker_label,
        origin=origin,
        start_sec=seg.start_sec,
        end_sec=seg.end_sec,
        text=seg.text,
        confidence=seg.confidence,
        absolute_start_utc=absolute,
    )
