"""議事録生成（L7）のテスト。Bedrock は client 注入で置き換え、AWS には触れない。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from subtext_postmeeting.config import PipelineConfig
from subtext_postmeeting.errors import PipelineError
from subtext_postmeeting.models import FinalTranscript, ResolvedSegment, StreamRole
from subtext_postmeeting.summarize import extract_markdown, read_stop_reason, summarize

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
