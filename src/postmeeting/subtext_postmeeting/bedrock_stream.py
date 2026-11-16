"""Bedrock(Claude messages) のストリーミング呼び出し。

出力上限まで生成すると応答全体の待ち時間が読み取りタイムアウトを超えうるため、要約段と補正段は
InvokeModelWithResponseStream で呼ぶ。差分は InvokeModel と同じ形の応答 JSON に組み立てて返し、
本文抽出・停止理由の読み取り（呼び出し側の純粋関数）は応答形式の違いを意識しない。

組み立てるのは本文（text ブロック）と `stop_reason` だけで、思考ブロックは捨てる。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any


def invoke_streaming(runtime: Any, model_id: str, body: str) -> bytes:
    """ストリーミングで推論し、InvokeModel 応答と同形の JSON（bytes）を返す。

    ストリーム中のエラーは botocore が反復時に EventStreamError（ClientError 派生）として送出する
    ため、ここでは捕捉せず呼び出し側の分類に任せる。
    """
    response = runtime.invoke_model_with_response_stream(modelId=model_id, body=body)
    events = (json.loads(item["chunk"]["bytes"]) for item in response["body"] if "chunk" in item)
    return json.dumps(assemble_message(events), ensure_ascii=False).encode("utf-8")


def assemble_message(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """ストリームイベント列から `{"content": [text ブロック...], "stop_reason": ...}` を組み立てる。"""
    texts: dict[int, list[str]] = {}
    stop_reason: str | None = None
    for event in events:
        kind = event.get("type")
        if kind == "content_block_start" and event["content_block"].get("type") == "text":
            texts.setdefault(event["index"], []).append(event["content_block"].get("text", ""))
        elif kind == "content_block_delta" and event["delta"].get("type") == "text_delta":
            texts.setdefault(event["index"], []).append(event["delta"]["text"])
        elif kind == "message_delta":
            stop_reason = event["delta"].get("stop_reason", stop_reason)
    content = [{"type": "text", "text": "".join(texts[index])} for index in sorted(texts)]
    return {"content": content, "stop_reason": stop_reason}
