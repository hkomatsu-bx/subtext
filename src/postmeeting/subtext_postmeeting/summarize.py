"""議事録生成（L7 / BR-SUM・FR-10/11）。

Bedrock(Claude) で「決定事項／ToDo（担当・期限・完了条件）／論点・議論サマリ／未決事項／要確認箇所」の
日本語 Markdown を生成する（BR-SUM-01）。入力は FinalTranscript のみ＝純粋変換（BR-SUM-02, FR-11）。
モデル ID・リージョンは設定由来（BR-SUM-04, FR-15）。partial 録音由来時は冒頭に注記（BR-SUM-05）。
文体・フォーマットの統一ルールは BR-SUM-07、創作禁止の具体化ルールは BR-SUM-08。
先頭の会議名・日時・参加者ヘッダーは BR-SUM-09（日時・参加者は FinalTranscript から決定的に算出し、
会議名はトランスクリプトに明示がある場合のみモデルが書く）。
"""

from __future__ import annotations

import json
import logging
import re
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

# 先頭ヘッダー

本文の5セクションより前に、次の順序でヘッダーを書いてください。

- 発言の中で会議そのものの名称が明示的に述べられている場合（例：「本日は◯◯様の定例会です」）にかぎり `# {会議名}` を1行目に書く。話題や社名から推測して書かない。明示が無ければこの行は書かない。
- `- 日時: {日時}` を1行。`[システム情報]` の日時をそのまま転記する（推測・言い換えをしない）。
- `- 参加者: {参加者}` を1行。`[システム情報]` の参加者をそのまま転記する。
- ヘッダーの後に空行を1行入れてから、5セクションを続ける。

# 出力の骨格

次の5セクションを、この順序で「## 見出し」として必ず含めてください。見出しの文言を変えず、他の見出しを追加しないでください。内容が無いセクションは省略せず「特になし」と書いてください。

## 決定事項
## ToDo
## 論点・議論サマリ
## 未決事項
## 要確認箇所

# 各セクションの書き方

## 決定事項

会議で確定し、以後の前提になるものだけを書きます。承認待ちのもの、一部の人の意見は決定ではありません。

形式：`- {決定内容}（{時刻}）`。時刻は必ず入れてください。

次の情報は、トランスクリプトで実際に述べられている場合にかぎり併記してください。述べられていない場合は項目ごと省きます（「前提: 記載なし」のようには書かない）。

- 前提となる条件：`前提: {条件}`
- 却下された案とその理由：`却下: {案}（{理由}）`
- 決定の適用範囲：`範囲: {範囲}`

## ToDo

1項目を次の形式で書きます。担当・期限・完了条件は1行にまとめず、チェックボックス行の下に1段インデントした箇条書きにしてください（1行に詰め込むと見落とされるため）。

```
- [ ] {内容}
    - 担当: {氏名または話者ラベル}
    - 期限: {YYYY-MM-DD}
    - 完了条件: {判定できる状態}
```

担当・期限がトランスクリプトで決まっていない場合は「担当: 未定」「期限: 未定」と明記してください（省略しない）。
期限は日付に変換してください（相対表現は会議日が判明していれば日付にし、判明しなければ発言のまま書いて「期限: 未定」を併記）。
完了条件は第三者が達成を判定できる形にしてください。
ToDo 項目同士の間には空行を1行入れてください。

## 論点・議論サマリ

論点別に構成します。論点ごとに `### {論点名}` を置き、3〜6行で書きます。同じ論点が会議中に何度も出てきた場合は一箇所にまとめます。
意見・懸念・提案には発言者（話者ラベルまたは氏名）を付けます。決定と事実報告には付けません。

## 未決事項

議題に上がったが結論に至らなかったものを書きます。結論が出ていない議論を、決定事項に格上げしないでください。

形式：`- {論点}（{時刻}）`。次の情報は、述べられている場合にかぎり併記します。

- 決定に必要な情報、または決定権者：`要: {内容}`
- 決まらない場合に止まるもの：`影響: {内容}`

このセクションが「特になし」になることはあり得ます。埋めるために論点を作らないでください。

## 要確認箇所

本文中の `[要確認]` をすべてここに集約します。形式：`- {時刻} {何が不明か}`。不明箇所が実際に無かった場合のみ「特になし」と書いてください。

# 創作の禁止

- トランスクリプトに根拠のない文を書かないでください。話の流れから自然に導ける結論であっても、誰も言っていないなら書きません。
- 聞き取り不明・意味不明の箇所は、その位置に `[要確認]` と書き、前後から推測して埋めないでください。
- 発言者ラベル（spk_0 等）はそのまま用いてください。**口調や呼びかけ、自己紹介の発言から話者を推定して人名を割り当てないでください。** 実名の確定は人手による話者名の記入だけで行います。
- 専門用語・製品名・数値が明らかに誤変換されている場合は、修正した上で原文表記を併記してください（例：`ECS（原文: イーシーエス）`）。黙って修正しないでください。
- 発言者が一度否定してから受け入れた、条件を付け加えたなど、立場の変更があった場合は、変更後の内容だけでなく変更があったことを残してください。

# 落とすもの、落とさないもの

落とす：フィラー、言い直し、雑談、会議運営上のやりとり、結論に影響しなかった脱線。
落とさない：数値、日付、固有名、条件付き合意の条件部分、懸念の表明とその発言者、却下された案、決まらなかったこと。

# 文体

- 常体で書きます。敬体（ですます）を使わないでください。
- 一文一項目。一つの文に二つの決定を入れないでください。一文は短く、主語と述語の対応を明確にしてください。
- 「〜することができます」「〜については」のような冗長な言い回しを避け、言い切ってください。
- 発言の要約は間接話法で書きます。原文の一人称や敬語を残さないでください。
- 箇条書きの記号は「- 」のみを使い、入れ子にしないでください（ToDo の担当・期限・完了条件だけは例外として1段インデントします）。論点・議論サマリの見出しには `### ` を使います。
- セクション（`## `見出し）の間には空行を1行入れてください。

# 書かない表現

- 状況の描写：「活発な議論が行われた」「認識合わせを実施した」「有意義な意見交換となった」
- 主語のない合意：「引き続き検討」「前向きに調整」「別途対応」。誰が何をいつまでにやるのかが無いため、ToDo か未決事項のどちらかに振り分けてください。
- 曖昧な列挙の閉じ方：「等」「など」、「その他所要の調整」
- 感情や態度の推測：「難色を示した」「乗り気だった」。発言内容だけを書きます。
- 書き手の評価：「妥当な判断である」。評価を入れないでください。

# 出力前の点検

- 決定事項の各行が、トランスクリプトの該当箇所と対応しているか。対応を確認できない行は削除する。
- すべての ToDo に担当・期限・完了条件があるか。不明なものが「未定」と明記されているか。
- 結論の出ていない論点が、決定事項に混ざっていないか。
- 話者ラベルを人名に置き換えていないか。
- 本文の `[要確認]` がすべて要確認箇所に集約されているか。"""


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
    """セグメントを「[mm:ss] 話者: テキスト」行に整形する（純粋・出典併記 BR-SUM-03）。

    日時・参加者は FinalTranscript から決定的に算出し `[システム情報]` として渡す（BR-SUM-09）。
    モデルに推測させず転記させるだけにすることで、日時・参加者ヘッダーの創作を防ぐ。
    """
    header = [
        "[システム情報]",
        f"セッション: {transcript.session_id}（言語: {transcript.language}）",
        f"日時: {_fmt_meeting_datetime(transcript)}",
        f"参加者: {'、'.join(transcript.speakers) if transcript.speakers else '記録なし'}",
        "",
    ]
    body = [f"[{_fmt_clock(seg.start_sec)}] {seg.speaker}: {seg.text}" for seg in transcript.segments]
    return "\n".join([*header, *body])


_SESSION_ID_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})\d{2}$")


def _fmt_meeting_datetime(transcript: FinalTranscript) -> str:
    """会議の日時を決定的に算出する（純粋・BR-SUM-09）。

    録音セッションは `sessionId`（`yyyyMMdd-HHmmss`、収録開始時刻）から算出する。
    一致しない場合（VTT 取込等）は `commonStartUtc` を使う。どちらも無ければ不明と明記する。
    """
    m = _SESSION_ID_RE.match(transcript.session_id)
    if m:
        y, mo, d, h, mi = m.groups()
        return f"{y}-{mo}-{d} {h}:{mi}"
    if transcript.common_start_utc is not None:
        return transcript.common_start_utc.strftime("%Y-%m-%d %H:%M UTC")
    return "不明"


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
