"""議事録生成（L7）のテスト。Bedrock は client 注入で置き換え、AWS には触れない。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from subtext_postmeeting.config import PipelineConfig
from subtext_postmeeting.errors import PipelineError
from subtext_postmeeting.models import FinalTranscript, MeetingInfo, ResolvedSegment, StreamRole
from subtext_postmeeting.summarize import (
    build_prompt,
    extract_markdown,
    format_transcript,
    read_stop_reason,
    summarize,
)

_MARKDOWN = "## 決定事項\n- A を承認\n\n## ToDo\n\n## 論点・議論サマリ\n- 予算"


def _config(tmp_path: Path) -> PipelineConfig:
    return PipelineConfig(
        aws_region="ap-northeast-1",
        s3_bucket="",
        s3_prefix="",
        language="ja-JP",
        max_speakers=5,
        poll_timeout_sec=60,
        keep_s3=False,
        output_dir=tmp_path,
        bedrock_model_id="fake-model",
        vocabulary_name="",
        correction_terms_path=tmp_path / "no-terms.json",
    )


def _transcript(*, is_partial: bool = False) -> FinalTranscript:
    return FinalTranscript(
        session_id="s1",
        language="ja-JP",
        segments=(
            ResolvedSegment(
                speaker="田中",
                origin=StreamRole.OTHERS,
                start_sec=1.0,
                end_sec=2.0,
                text="おはよう",
                confidence=None,
                absolute_start_utc=None,
            ),
        ),
        speakers=("田中",),
        common_start_utc=None,
        model_info={},
        is_partial=is_partial,
    )


class _FakeRuntime:
    """invoke_model の応答を固定で返すフェイク（stop_reason を差し替えられる）。"""

    def __init__(self, *, stop_reason: str = "end_turn", markdown: str = _MARKDOWN) -> None:
        self._payload = json.dumps(
            {"content": [{"type": "text", "text": markdown}], "stop_reason": stop_reason}
        ).encode("utf-8")
        self.calls = 0

    def invoke_model(self, *, modelId: str, body: str) -> dict[str, Any]:
        self.calls += 1
        return {"body": _FakeBody(self._payload)}


class _FakeBody:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


def _summarize(tmp_path: Path, runtime: _FakeRuntime, *, is_partial: bool = False):
    return summarize(
        _transcript(is_partial=is_partial),
        _config(tmp_path),
        client=runtime,
        now=lambda: datetime(2026, 6, 20, tzinfo=timezone.utc),
    )


def test_summarize_returns_markdown_without_notes(tmp_path: Path) -> None:
    doc = _summarize(tmp_path, _FakeRuntime())

    assert doc.markdown == _MARKDOWN
    assert doc.source_model == "fake-model"


def test_summarize_flags_output_truncated_at_max_tokens(tmp_path: Path) -> None:
    """出力上限で打ち切られた議事録に注記を付けること。

    `stop_reason` を見ないと、文の途中で終わった議事録が COMPLETED として minutes.md に
    書かれ、そのまま Slack へ投稿される（本文だけ見ると Markdown として成立してしまう）。
    注記は本文に載せて成果物と一緒に持ち回らせる（Slack にもそのまま届く）。
    """
    doc = _summarize(tmp_path, _FakeRuntime(stop_reason="max_tokens"))

    assert "途中で切れています" in doc.markdown
    assert doc.markdown.endswith(_MARKDOWN), "本文自体は保持すること（課金済みの成果を捨てない）"


def test_summarize_keeps_partial_note_together_with_truncation_note(tmp_path: Path) -> None:
    doc = _summarize(tmp_path, _FakeRuntime(stop_reason="max_tokens"), is_partial=True)

    assert "部分録音" in doc.markdown
    assert "途中で切れています" in doc.markdown


def test_read_stop_reason_returns_none_for_unparsable_payload() -> None:
    assert read_stop_reason(b"not json") is None
    assert read_stop_reason(json.dumps({"content": []})) is None


def test_extract_markdown_raises_when_no_text() -> None:
    with pytest.raises(PipelineError):
        extract_markdown(json.dumps({"content": [{"type": "text", "text": "  "}]}))


# ---------------------------------------------------------------------------
# 会議情報（MeetingInfo）の反映（BR-MI-01）
# ---------------------------------------------------------------------------
def test_format_transcript_without_meeting_info_matches_legacy_fallback() -> None:
    """meeting_info 未指定は現行の決定的算出のまま（回帰）。"""
    with_none = format_transcript(_transcript(), None)
    without_arg = format_transcript(_transcript())

    assert with_none == without_arg
    assert "会議名:" not in with_none
    assert "参加者: 田中" in with_none


def test_format_transcript_empty_meeting_info_is_same_as_none() -> None:
    empty = MeetingInfo()

    assert format_transcript(_transcript(), empty) == format_transcript(_transcript(), None)


def test_format_transcript_applies_meeting_info_overrides() -> None:
    info = MeetingInfo(title="定例会", meeting_datetime="2026-08-26 10:00", participants=("田中", "山田（A社）"))

    text = format_transcript(_transcript(), info)

    assert "会議名: 定例会" in text
    assert "日時: 2026-08-26 10:00" in text
    assert "参加者: 田中、山田（A社）" in text


def test_format_transcript_meeting_info_without_title_omits_title_line() -> None:
    info = MeetingInfo(meeting_datetime="2026-08-26 10:00")

    text = format_transcript(_transcript(), info)

    assert "会議名:" not in text
    assert "日時: 2026-08-26 10:00" in text


def test_build_prompt_includes_meeting_info_before_transcript_header() -> None:
    info = MeetingInfo(title="定例会")

    prompt = build_prompt(_transcript(), info)

    assert "会議名: 定例会" in prompt
    assert prompt.index("会議名: 定例会") < prompt.index("[00:01]")


def test_summarize_passes_meeting_info_through_to_prompt() -> None:
    runtime = _FakeRuntime()
    config = _config(Path("."))
    doc = summarize(
        _transcript(),
        config,
        client=runtime,
        now=lambda: datetime(2026, 6, 20, tzinfo=timezone.utc),
        meeting_info=MeetingInfo(title="定例会"),
    )

    assert doc.markdown == _MARKDOWN  # フェイク応答は固定。呼び出しが例外なく通ることを確認する。
    assert runtime.calls == 1


# ---------------------------------------------------------------------------
# 付帯資料（materials_text）の反映（③付帯資料）
# ---------------------------------------------------------------------------
def test_format_transcript_without_materials_omits_reference_block() -> None:
    text = format_transcript(_transcript())
    assert "[参考資料]" not in text
    assert "[発言記録]" in text


def test_format_transcript_places_materials_between_header_and_transcript() -> None:
    text = format_transcript(_transcript(), materials_text="[参考資料]\n--- 資料1: a.txt（2文字）---\n本文")
    header_end = text.index("参加者:")
    materials_pos = text.index("[参考資料]")
    body_pos = text.index("[発言記録]")
    assert header_end < materials_pos < body_pos


def test_build_prompt_includes_materials_text() -> None:
    prompt = build_prompt(_transcript(), materials_text="[参考資料]\n--- 資料1: a.txt（2文字）---\n本文")
    assert "[参考資料]" in prompt
    assert "本文" in prompt


def test_summarize_passes_materials_text_through_to_prompt() -> None:
    runtime = _FakeRuntime()
    config = _config(Path("."))
    doc = summarize(
        _transcript(),
        config,
        client=runtime,
        now=lambda: datetime(2026, 6, 20, tzinfo=timezone.utc),
        materials_text="[参考資料]\n--- 資料1: a.txt（2文字）---\n本文",
    )
    assert doc.markdown == _MARKDOWN
    assert runtime.calls == 1


# ---------------------------------------------------------------------------
# 議事録プロンプトの文体規則（japanese-tech-writing のエッセイ取り込み）
# ---------------------------------------------------------------------------
def test_instruction_bans_llm_filler_phrases() -> None:
    from subtext_postmeeting.summarize import _INSTRUCTION

    assert "重要なのは" in _INSTRUCTION
    assert "多角的に" in _INSTRUCTION
    assert "掘り下げる" in _INSTRUCTION
    assert "と言えるだろう" in _INSTRUCTION


def test_instruction_requires_avoiding_repetition_and_conflation() -> None:
    """重複禁止の射程は「同じ文の再掲」に限る（決定の経緯を論点サマリで述べる余地を残す）。"""
    from subtext_postmeeting.summarize import _INSTRUCTION

    assert "同じ文をそのまま別のセクションへ再掲しないでください" in _INSTRUCTION
    assert "決定に至る経緯を論点・議論サマリで述べることは重複ではありません" in _INSTRUCTION
    assert "腑分け" in _INSTRUCTION


def test_instruction_forbids_rewriting_numbers_from_materials() -> None:
    """資料と発言で数値が食い違う場合に発言を優先させる（合意内容が資料で上書きされるのを防ぐ）。"""
    from subtext_postmeeting.summarize import _INSTRUCTION

    assert "数値は資料で書き換えないでください" in _INSTRUCTION
    assert "発言の値を採用" in _INSTRUCTION


def test_instruction_keeps_confirmation_markers_in_body() -> None:
    """要確認箇所は本文から移動させず一覧として集約する（本文のマーカーを消さない）。"""
    from subtext_postmeeting.summarize import _INSTRUCTION

    assert "本文に残したまま" in _INSTRUCTION


def test_instruction_allows_indent_for_all_attribute_lines() -> None:
    """1段インデントの例外は ToDo だけでなく決定事項・未決事項の付帯情報にも及ぶ。"""
    from subtext_postmeeting.summarize import _INSTRUCTION

    assert "決定事項の前提・却下・範囲、未決事項の要・影響" in _INSTRUCTION


def test_instruction_limits_heading_ban_to_level_two() -> None:
    """`# 会議名` と `### 論点名` を禁止対象から除く（骨格の見出し規則との衝突を避ける）。"""
    from subtext_postmeeting.summarize import _INSTRUCTION

    assert "この5つ以外の `## ` 見出しを追加しないでください" in _INSTRUCTION


def test_instruction_preserves_speaker_own_hedging_and_enumeration() -> None:
    """発言者自身の「など」「かもしれない」は創作防止のため保持させる。"""
    from subtext_postmeeting.summarize import _INSTRUCTION

    assert "発言者自身が「A、Bなど」と言った場合" in _INSTRUCTION
    assert "発言者自身が「かもしれない」と言った場合" in _INSTRUCTION


def test_instruction_checklist_covers_materials() -> None:
    from subtext_postmeeting.summarize import _INSTRUCTION

    checklist = _INSTRUCTION.split("# 出力前の点検")[1]
    assert "資料にしか無い事実・数値・予定が本文に混ざっていないか" in checklist
    assert "（資料: {ファイル名}）` の付記があるか" in checklist
