"""Bedrock ストリーミング応答の組み立てのテスト。runtime は注入し、AWS には触れない。"""

from __future__ import annotations

import json
from typing import Any

from subtext_postmeeting.bedrock_stream import invoke_streaming


def _chunk(event: dict[str, Any]) -> dict[str, Any]:
    return {"chunk": {"bytes": json.dumps(event).encode("utf-8")}}


class _FakeStreamRuntime:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events
        self.calls: list[tuple[str, str]] = []

    def invoke_model_with_response_stream(self, *, modelId: str, body: str) -> dict[str, Any]:  # noqa: N803
        self.calls.append((modelId, body))
        return {"body": iter(self._events)}


def _events_with_thinking_then_text(stop_reason: str) -> list[dict[str, Any]]:
    return [
        _chunk({"type": "message_start", "message": {"content": []}}),
        _chunk({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}}),
        _chunk({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "考え中"}}),
        _chunk({"type": "content_block_stop", "index": 0}),
        _chunk({"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}}),
        _chunk({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "## 決定"}}),
        _chunk({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "事項"}}),
        _chunk({"type": "content_block_stop", "index": 1}),
        _chunk({"type": "message_delta", "delta": {"stop_reason": stop_reason}}),
        _chunk({"type": "message_stop"}),
    ]


def test_assembles_text_deltas_into_invoke_model_shaped_payload() -> None:
    runtime = _FakeStreamRuntime(_events_with_thinking_then_text("end_turn"))

    payload = json.loads(invoke_streaming(runtime, "m", "{}"))

    assert payload == {"content": [{"type": "text", "text": "## 決定事項"}], "stop_reason": "end_turn"}


def test_passes_model_and_body_through() -> None:
    runtime = _FakeStreamRuntime(_events_with_thinking_then_text("end_turn"))

    invoke_streaming(runtime, "jp.anthropic.claude-haiku-5-5", '{"x": 1}')

    assert runtime.calls == [("jp.anthropic.claude-haiku-5-5", '{"x": 1}')]


def test_thinking_only_response_has_no_text_but_keeps_stop_reason() -> None:
    """思考だけで上限を使い切った応答。本文は空で、停止理由 max_tokens を保持する。"""
    events = [
        _chunk({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}}),
        _chunk({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "…"}}),
        _chunk({"type": "message_delta", "delta": {"stop_reason": "max_tokens"}}),
    ]

    payload = json.loads(invoke_streaming(_FakeStreamRuntime(events), "m", "{}"))

    assert payload == {"content": [], "stop_reason": "max_tokens"}


def test_multiple_text_blocks_keep_index_order() -> None:
    events = [
        _chunk({"type": "content_block_start", "index": 2, "content_block": {"type": "text", "text": "B"}}),
        _chunk({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "A"}}),
        _chunk({"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
    ]

    payload = json.loads(invoke_streaming(_FakeStreamRuntime(events), "m", "{}"))

    assert [block["text"] for block in payload["content"]] == ["A", "B"]
