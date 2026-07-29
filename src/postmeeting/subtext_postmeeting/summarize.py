"""議事録生成（L7 / BR-SUM・FR-10/11）。

Bedrock(Claude) で「決定事項／ToDo（担当・期限）／論点・議論サマリ」の日本語 Markdown を生成する
（BR-SUM-01）。入力は FinalTranscript のみ＝純粋変換（BR-SUM-02, FR-11）。モデル ID・リージョンは
設定由来（BR-SUM-04, FR-15）。partial 録音由来時は冒頭に注記（BR-SUM-05）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from .aws import build_client
from .config import PipelineConfig
from .errors import PipelineError
from .models import FinalTranscript, MinutesDoc

logger = logging.getLogger("subtext.postmeeting")

_STAGE = "summarize"
_MAX_TOKENS = 4096
_ANTHROPIC_VERSION = "bedrock-2023-05-31"
# Bedrock(Claude messages) の停止理由。出力上限で打ち切られたことを示す値。
_STOP_REASON_MAX_TOKENS = "max_tokens"

_PARTIAL_NOTE = "> 注記: この議事録は部分録音（録音が途中で終了した可能性のあるセッション）から生成されています。"
_TRUNCATED_NOTE = (
    "> ⚠ 注記: この議事録はモデルの出力上限に達して**途中で切れています**"
    "（末尾のセクションが欠けている可能性があります）。全文は final_transcript.json を参照してください。"
)

_INSTRUCTION = """あなたは会議の書記です。以下の会議トランスクリプトから、日本語の Markdown 議事録を作成してください。

必ず次の3セクションを「## 見出し」で含めてください:

## 決定事項
会議で決まったことを箇条書きで。可能なら発言者・時刻を併記。

## ToDo
対応事項を「- [ ] 内容（担当: 氏名 / 期限: 日付）」形式で。担当・期限が不明な場合はその旨を明記。

## 論点・議論サマリ
主要な論点と議論の流れを簡潔に。

トランスクリプトに無い情報を創作しないこと。発言者ラベル（spk_0 等）が残っている場合はそのまま用いること。"""


def summarize(
    transcript: FinalTranscript,
    config: PipelineConfig,
    client: Any | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> MinutesDoc:
    """FinalTranscript から議事録を生成する。client/now は注入可能（テスト容易性）。"""
    model_id = config.require_bedrock_model()
    prompt = build_prompt(transcript)
    runtime = client if client is not None else build_client("bedrock-runtime", config.aws_region, stage=_STAGE)

    body = json.dumps(
        {
            "anthropic_version": _ANTHROPIC_VERSION,
            "max_tokens": _MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        }
    )
    try:
        response = runtime.invoke_model(modelId=model_id, body=body)
        payload = response["body"].read()
    except Exception as exc:
        raise PipelineError("Bedrock 推論呼び出しに失敗しました。", failed_stage=_STAGE) from exc

    markdown = extract_markdown(payload)
    if read_stop_reason(payload) == _STOP_REASON_MAX_TOKENS:
        # 打ち切りを検知しないと、文の途中で終わった議事録が「完了」として minutes.md に書かれ、
        # そのまま Slack へ投稿される。本文に注記を載せて成果物と一緒に持ち回らせる。
        logger.warning("議事録が出力上限で打ち切られました（max_tokens=%d）。注記を付与します。", _MAX_TOKENS)
        markdown = f"{_TRUNCATED_NOTE}\n\n{markdown}"
    if transcript.is_partial:
        markdown = f"{_PARTIAL_NOTE}\n\n{markdown}"  # BR-SUM-05

    return MinutesDoc(
        session_id=transcript.session_id,
        markdown=markdown,
        source_model=model_id,
        generated_at_utc=now(),
    )


def build_prompt(transcript: FinalTranscript) -> str:
    """指示＋トランスクリプト本文からプロンプトを組み立てる（純粋, BR-SUM-02/03）。"""
    return f"{_INSTRUCTION}\n\n---\n\n{format_transcript(transcript)}"


def format_transcript(transcript: FinalTranscript) -> str:
    """セグメントを「[mm:ss] 話者: テキスト」行に整形する（純粋・出典併記 BR-SUM-03）。"""
    header = [f"会議セッション: {transcript.session_id}（言語: {transcript.language}）", ""]
    body = [f"[{_fmt_clock(seg.start_sec)}] {seg.speaker}: {seg.text}" for seg in transcript.segments]
    return "\n".join([*header, *body])


def extract_markdown(payload: bytes | str) -> str:
    """Bedrock(Claude messages) 応答から本文テキストを取り出す（純粋）。"""
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as exc:
        raise PipelineError("Bedrock 応答の解析に失敗しました。", failed_stage=_STAGE) from exc

    content = data.get("content", [])
    texts = [block.get("text", "") for block in content if block.get("type") == "text"]
    markdown = "".join(texts).strip()
    if not markdown:
        raise PipelineError("Bedrock 応答にテキストが含まれていません。", failed_stage=_STAGE)
    return markdown


def read_stop_reason(payload: bytes | str) -> str | None:
    """Bedrock(Claude messages) 応答の `stop_reason` を返す（純粋・取得不能なら None）。

    `max_tokens` は出力が上限で打ち切られたことを意味する。本文だけ見ていると Markdown として
    成立してしまうため、ここで明示的に読む（見落とすと途中で切れた議事録が正常扱いになる）。
    """
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return None  # 本文抽出側（extract_markdown）が actionable な例外を出す。
    reason = data.get("stop_reason") if isinstance(data, dict) else None
    return str(reason) if reason is not None else None


def _fmt_clock(seconds: float) -> str:
    total = int(max(0.0, seconds))
    return f"{total // 60:02d}:{total % 60:02d}"
