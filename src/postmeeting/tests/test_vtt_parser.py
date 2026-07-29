"""VTT 入力パースのテスト（VTT 経路）。

Teams が出力する WebVTT を Transcribe を経由せず FinalTranscript に変換する純粋関数を
検証する。話者ラベルは出現順 spk_n、VTT 話者名は model_info の speakerNameHints に保持。
"""

from __future__ import annotations

import pytest

from subtext_postmeeting.errors import PipelineError
from subtext_postmeeting.models import StreamRole
from subtext_postmeeting.vtt_parser import parse_vtt

_TEAMS_VTT = """WEBVTT

1
00:00:01.000 --> 00:00:04.000
<v 田中太郎>おはようございます。</v>

2
00:00:05.500 --> 00:00:08.250
<v 佐藤花子>本日はよろしくお願いします。</v>

3
00:00:09.000 --> 00:00:12.000
<v 田中太郎>では始めましょう。</v>
"""


class TestParseVtt:
    def test_builds_final_transcript_with_segments(self) -> None:
        ft = parse_vtt(_TEAMS_VTT, "meeting", "ja-JP")
        assert len(ft.segments) == 3
        assert ft.segments[0].text == "おはようございます。"
        assert ft.segments[0].origin == StreamRole.OTHERS
        assert ft.segments[0].start_sec == pytest.approx(1.0)
        assert ft.segments[0].end_sec == pytest.approx(4.0)
        assert ft.is_partial is False
        assert ft.common_start_utc is None

    def test_assigns_stable_labels_in_appearance_order(self) -> None:
        ft = parse_vtt(_TEAMS_VTT, "meeting", "ja-JP")
        # 田中=spk_0 / 佐藤=spk_1 / 3つ目は田中に戻り spk_0。
        assert ft.segments[0].speaker == "spk_0"
        assert ft.segments[1].speaker == "spk_1"
        assert ft.segments[2].speaker == "spk_0"
        assert ft.speakers == ("spk_0", "spk_1")

    def test_carries_speaker_name_hints(self) -> None:
        ft = parse_vtt(_TEAMS_VTT, "meeting", "ja-JP")
        assert ft.model_info["source"] == "vtt"
        assert ft.model_info["speakerNameHints"] == {
            "spk_0": "田中太郎",
            "spk_1": "佐藤花子",
        }

    def test_handles_multiline_and_inline_tags(self) -> None:
        vtt = "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\n<v 田中>こんにちは\n世界</v>\n"
        ft = parse_vtt(vtt, "m", "ja-JP")
        assert ft.segments[0].text == "こんにちは 世界"

    def test_cue_without_voice_tag_gets_label_without_hint(self) -> None:
        vtt = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nナレーション\n"
        ft = parse_vtt(vtt, "m", "ja-JP")
        assert ft.segments[0].speaker == "spk_0"
        assert ft.segments[0].text == "ナレーション"
        assert ft.model_info.get("speakerNameHints", {}) == {}

    def test_accepts_mm_ss_and_comma_millis(self) -> None:
        vtt = "WEBVTT\n\n01:02.500 --> 01:05,000\n<v A>x</v>\n"
        ft = parse_vtt(vtt, "m", "ja-JP")
        assert ft.segments[0].start_sec == pytest.approx(62.5)
        assert ft.segments[0].end_sec == pytest.approx(65.0)

    def test_skips_note_blocks(self) -> None:
        vtt = (
            "WEBVTT\n\n"
            "NOTE これはメモ。タイムスタンプを持たないブロック。\n\n"
            "00:00:00.000 --> 00:00:01.000\n<v A>はい</v>\n"
        )
        ft = parse_vtt(vtt, "m", "ja-JP")
        assert len(ft.segments) == 1
        assert ft.segments[0].text == "はい"

    def test_empty_or_cueless_raises(self) -> None:
        with pytest.raises(PipelineError):
            parse_vtt("WEBVTT\n\nNOTE just a note\n", "m", "ja-JP")
