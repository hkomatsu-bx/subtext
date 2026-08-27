"""結果パースのテスト（BR-PARSE）。純粋関数を重点的に検証する。"""

from __future__ import annotations

from subtext_postmeeting.models import StreamRole
from subtext_postmeeting.parser import parse

from .conftest import others_transcribe_json, self_transcribe_json


class TestParseOthers:
    def test_splits_segments_by_speaker_boundary(self) -> None:
        # Arrange
        raw = others_transcribe_json()
        # Act
        result = parse(raw, StreamRole.OTHERS, "sess1", "ja-JP")
        # Assert
        assert [s.speaker_label for s in result.segments] == ["spk_0", "spk_1"]

    def test_attaches_punctuation_to_preceding_speaker(self) -> None:
        result = parse(others_transcribe_json(), StreamRole.OTHERS, "sess1", "ja-JP")
        assert result.segments[0].text == "おはよう。"

    def test_concatenates_words_within_speaker_segment(self) -> None:
        result = parse(others_transcribe_json(), StreamRole.OTHERS, "sess1", "ja-JP")
        assert result.segments[1].text == "こんにちはございます"

    def test_confidence_is_mean_of_pronunciation_items(self) -> None:
        result = parse(others_transcribe_json(), StreamRole.OTHERS, "sess1", "ja-JP")
        # spk_1 の信頼度平均 = (0.8 + 0.7) / 2
        assert result.segments[1].confidence == 0.75

    def test_segment_times_span_member_words(self) -> None:
        result = parse(others_transcribe_json(), StreamRole.OTHERS, "sess1", "ja-JP")
        spk1 = result.segments[1]
        assert spk1.start_sec == 3.0
        assert spk1.end_sec == 4.0


class TestParseSelf:
    def test_all_segments_labeled_self(self) -> None:
        result = parse(self_transcribe_json(), StreamRole.SELF, "sess1", "ja-JP")
        assert {s.speaker_label for s in result.segments} == {"self"}

    def test_pause_splits_into_two_segments(self) -> None:
        # 0.6→5.0 のギャップ(4秒)で分割される（しきい値1.0秒）
        result = parse(self_transcribe_json(), StreamRole.SELF, "sess1", "ja-JP")
        assert len(result.segments) == 2
        assert result.segments[0].text == "はいそうです。"
        assert result.segments[1].text == "では"


class TestParseEdgeCases:
    def test_empty_items_yields_no_segments(self) -> None:
        result = parse({"results": {"items": []}}, StreamRole.SELF, "s", "ja-JP")
        assert result.segments == ()

    def test_missing_confidence_results_in_none(self) -> None:
        raw = {
            "results": {
                "items": [
                    {
                        "type": "pronunciation",
                        "start_time": "0.0",
                        "end_time": "0.5",
                        "alternatives": [{"content": "あ"}],
                    }
                ]
            }
        }
        result = parse(raw, StreamRole.SELF, "s", "ja-JP")
        assert result.segments[0].confidence is None


class TestWordSeparator:
    """分かち書き言語では語間に空白を入れる（L-3: `LANGUAGE=en-US` で語が潰れないこと）。"""

    def test_japanese_keeps_words_unspaced(self) -> None:
        result = parse(others_transcribe_json(), StreamRole.OTHERS, "s", "ja-JP")
        assert result.segments[1].text == "こんにちはございます"

    def test_english_joins_words_with_space(self) -> None:
        raw = {
            "results": {
                "items": [
                    {
                        "type": "pronunciation",
                        "start_time": "0.0",
                        "end_time": "0.4",
                        "alternatives": [{"confidence": "0.9", "content": "hello"}],
                    },
                    {
                        "type": "pronunciation",
                        "start_time": "0.4",
                        "end_time": "0.8",
                        "alternatives": [{"confidence": "0.9", "content": "world"}],
                    },
                    {"type": "punctuation", "alternatives": [{"content": "."}]},
                ]
            }
        }

        result = parse(raw, StreamRole.SELF, "s", "en-US")

        # 語間は空白、句読点は直前の語へ密着する。
        assert result.segments[0].text == "hello world."

    def test_language_matching_is_case_insensitive_and_prefix_based(self) -> None:
        result = parse(others_transcribe_json(), StreamRole.OTHERS, "s", "JA-jp")
        assert result.segments[1].text == "こんにちはございます"
