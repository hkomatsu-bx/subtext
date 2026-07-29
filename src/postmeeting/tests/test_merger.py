"""統合のテスト（BR-MERGE）。絶対時刻・昇順マージ・純粋性を検証する。"""

from __future__ import annotations

from datetime import datetime, timezone

from subtext_postmeeting.merger import merge
from subtext_postmeeting.models import RawTranscript, StreamRole, TranscriptSegment


def _raw(role: StreamRole, segs: list[tuple[float, float, str, str]]) -> RawTranscript:
    return RawTranscript(
        role=role,
        session_id="sess1",
        language="ja-JP",
        segments=tuple(
            TranscriptSegment(speaker_label=lbl, start_sec=a, end_sec=b, text=t, confidence=0.9)
            for (a, b, t, lbl) in segs
        ),
    )


class TestMergeOrdering:
    def test_segments_sorted_by_start_sec(self) -> None:
        others = _raw(StreamRole.OTHERS, [(3.0, 4.0, "そうだね", "spk_0")])
        self_raw = _raw(StreamRole.SELF, [(0.0, 1.0, "おはよう", "self")])
        result = merge(others, self_raw, None)
        assert [round(s.start_sec, 1) for s in result.segments] == [0.0, 3.0]

    def test_same_time_others_first(self) -> None:
        others = _raw(StreamRole.OTHERS, [(1.0, 2.0, "A", "spk_0")])
        self_raw = _raw(StreamRole.SELF, [(1.0, 2.0, "B", "self")])
        result = merge(others, self_raw, None)
        assert result.segments[0].origin == StreamRole.OTHERS


class TestAbsoluteTime:
    def test_absolute_start_added_when_common_start_present(self) -> None:
        anchor = datetime(2026, 6, 20, 1, 0, 0, tzinfo=timezone.utc)
        others = _raw(StreamRole.OTHERS, [(10.0, 11.0, "X", "spk_0")])
        result = merge(others, None, anchor)
        assert result.segments[0].absolute_start_utc == datetime(2026, 6, 20, 1, 0, 10, tzinfo=timezone.utc)

    def test_absolute_start_none_when_no_anchor(self) -> None:
        others = _raw(StreamRole.OTHERS, [(10.0, 11.0, "X", "spk_0")])
        result = merge(others, None, None)
        assert result.segments[0].absolute_start_utc is None


class TestMergeProperties:
    def test_speakers_distinct_in_order(self) -> None:
        others = _raw(StreamRole.OTHERS, [(0.0, 1.0, "a", "spk_0"), (2.0, 3.0, "b", "spk_1")])
        self_raw = _raw(StreamRole.SELF, [(1.0, 2.0, "c", "self")])
        result = merge(others, self_raw, None)
        assert result.speakers == ("spk_0", "self", "spk_1")

    def test_pure_same_input_same_output(self) -> None:
        others = _raw(StreamRole.OTHERS, [(0.0, 1.0, "a", "spk_0")])
        first = merge(others, None, None)
        second = merge(others, None, None)
        assert first.to_json() == second.to_json()

    def test_single_mode_passes_others_only(self) -> None:
        others = _raw(StreamRole.OTHERS, [(0.0, 1.0, "a", "spk_0")])
        result = merge(others, None, None)
        assert len(result.segments) == 1
        assert result.segments[0].origin == StreamRole.OTHERS

    def test_partial_flag_propagates(self) -> None:
        others = _raw(StreamRole.OTHERS, [(0.0, 1.0, "a", "spk_0")])
        result = merge(others, None, None, is_partial=True)
        assert result.is_partial is True
