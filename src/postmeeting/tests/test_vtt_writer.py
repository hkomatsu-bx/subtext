"""WebVTT 出力のテスト（mp4→VTT ツール）。"""

from __future__ import annotations

from subtext_postmeeting.models import RawTranscript, StreamRole, TranscriptSegment
from subtext_postmeeting.vtt_parser import parse_vtt
from subtext_postmeeting.vtt_writer import to_vtt


def _raw(*segs: TranscriptSegment) -> RawTranscript:
    return RawTranscript(role=StreamRole.OTHERS, session_id="s", language="ja-JP", segments=tuple(segs))


class TestToVtt:
    def test_emits_header_timestamps_and_voice_tags(self) -> None:
        rt = _raw(
            TranscriptSegment(speaker_label="spk_0", start_sec=1.0, end_sec=4.5, text="おはよう"),
            TranscriptSegment(speaker_label="spk_1", start_sec=5.0, end_sec=6.25, text="こんにちは"),
        )
        vtt = to_vtt(rt)
        assert vtt.startswith("WEBVTT\n")
        assert "00:00:01.000 --> 00:00:04.500" in vtt
        assert "<v spk_0>おはよう</v>" in vtt
        assert "00:00:05.000 --> 00:00:06.250" in vtt
        assert "<v spk_1>こんにちは</v>" in vtt

    def test_escapes_special_chars(self) -> None:
        rt = _raw(TranscriptSegment(speaker_label="spk_0", start_sec=0.0, end_sec=1.0, text="a<b>&c"))
        vtt = to_vtt(rt)
        assert "a&lt;b&gt;&amp;c" in vtt

    def test_formats_hours(self) -> None:
        rt = _raw(TranscriptSegment(speaker_label="spk_0", start_sec=3661.0, end_sec=3662.0, text="x"))
        assert "01:01:01.000 --> 01:01:02.000" in to_vtt(rt)

    def test_roundtrips_escaped_text_without_corruption(self) -> None:
        """エスケープした本文が読み戻しで元へ戻ること（writer/parser の対称性）。

        戻さないと `A&B の 5 < 10` が `A&amp;B の 5 &lt; 10` のまま final_transcript.json →
        議事録 → Slack まで流れ、恒久的に壊れる。mp4/ライブ経路と Teams VTT 入力の全てが対象。
        """
        original = "A&B の 5 < 10 の話 <重要>"
        rt = _raw(TranscriptSegment(speaker_label="spk_0", start_sec=0.0, end_sec=1.0, text=original))

        ft = parse_vtt(to_vtt(rt), "s", "ja-JP")

        assert [s.text for s in ft.segments] == [original]

    def test_roundtrips_speaker_name_with_ampersand(self) -> None:
        """話者名の実体参照も戻すこと（命名ゲートの初期値・議事録の話者ラベルになる）。"""
        vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v Smith &amp; Sons>hello</v>\n"

        ft = parse_vtt(vtt, "s", "ja-JP")

        assert ft.model_info["speakerNameHints"] == {"spk_0": "Smith & Sons"}

    def test_roundtrips_through_parser(self) -> None:
        # writer → parser で話者構造が保たれる（spk ラベルは機械ラベルなのでヒント化されない）。
        rt = _raw(
            TranscriptSegment(speaker_label="spk_0", start_sec=1.0, end_sec=2.0, text="あ"),
            TranscriptSegment(speaker_label="spk_1", start_sec=3.0, end_sec=4.0, text="い"),
            TranscriptSegment(speaker_label="spk_0", start_sec=5.0, end_sec=6.0, text="う"),
        )
        ft = parse_vtt(to_vtt(rt), "s", "ja-JP")
        assert [s.speaker for s in ft.segments] == ["spk_0", "spk_1", "spk_0"]
        assert ft.model_info.get("speakerNameHints", {}) == {}  # 機械ラベルは初期値にしない
