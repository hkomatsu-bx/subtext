"""Slack 投稿（chat.postMessage）。議事録を親メッセージ＋全文スレッドで投稿する。

方針:
  - 依存追加なし（ハーネスの stdlib-only 原則を維持）。HTTP は標準ライブラリ urllib で叩き、
    トランスポートは `poster` seam として注入可能にする（テストはネットワークに触れない）。
  - 親メッセージは minutes.md から `## 決定事項` / `## ToDo` セクションを抽出する決定論的生成
    （LLM 不使用）。全文はスレッド返信へ行境界で分割して連投する。
  - 投稿本文は Slack の mrkdwn へ変換する。Slack は標準 Markdown を解釈しないため、GFM の
    見出し `#`・太字 `**` ・チェックボックス `- [ ]` をそのまま送ると記号が露出する（`to_mrkdwn`）。
  - Bot Token は秘密。値はログ/print に一切出さない（BR-SEC-01）。環境変数 → ルート .env の
    順で解決する（`.env` はプロジェクトの設定集約先・gitignore 済）。
  - Slack API の論理エラー（ok:false）は握りつぶさず ValueError で停止する。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

from meeting.config import read_env_value

# chat.postMessage エンドポイント。
_SLACK_POST_URL = "https://slack.com/api/chat.postMessage"
# HTTP タイムアウト（秒）。
_HTTP_TIMEOUT_SEC = 15.0
# 1メッセージあたりの安全な文字数上限（Slack の text 上限は大きいが可読性重視で抑える）。
_CHUNK_LIMIT = 3800
# 親メッセージの決定事項/ToDo セクション見出しキーワード（minutes.md の統一見出しに追従）。
_DECISION_KEYWORD = "決定事項"
_TODO_KEYWORD = "ToDo"
# 見出しが無い議事録の親フォールバックに載せる先頭本文の文字数。
_PARENT_FALLBACK_CHARS = 1500
# 親メッセージ末尾の脚注（全文はスレッドに続く旨）。
_PARENT_FOOTER = "📄 詳細な議事録全文はこのスレッドに続けて投稿します。"
# Bot Token の環境変数名。
_TOKEN_ENV = "SLACK_BOT_TOKEN"
# 全文スレッドの連投間隔（秒）。chat.postMessage は概ね毎秒1通が目安で、長い議事録（6通以上）を
# 待機なしで投げるとレート制限に当たる。途中で失敗するとスレッドが半端に公開されたまま残り、
# 再実行では親メッセージから重複する（取り消せない外部公開なので投げ切ることを優先する）。
_CHUNK_INTERVAL_SEC = 1.0
# レート制限に当たったときの再試行回数（Retry-After に従う）。
_MAX_RATELIMIT_RETRIES = 3
# Retry-After が無い/壊れている場合の待機秒と、待機の上限（暴走待機を避ける）。
_DEFAULT_RETRY_AFTER_SEC = 5.0
_MAX_RETRY_AFTER_SEC = 60.0
# レート制限を表す Slack エラーコード。
_RATELIMITED = "ratelimited"
# `_http_post` が HTTP ヘッダの Retry-After を応答 dict へ載せるキー（poster seam を跨いで渡す）。
_RETRY_AFTER_KEY = "retry_after"

# トランスポート seam。(token, payload) を受けて Slack 応答 dict を返す。
Poster = Callable[[str, Mapping[str, Any]], dict[str, Any]]
# 待機の seam（テストは実際に待たない）。
Sleeper = Callable[[float], None]


# ---------------------------------------------------------------------------
# トークン解決（環境変数 → ルート .env）
# ---------------------------------------------------------------------------
def resolve_slack_token(repo_root: Path, env: Mapping[str, str]) -> str | None:
    """Bot Token を解決する。環境変数優先、無ければルート .env。無ければ None。

    値はここでも呼び出し側でもログ/print に出さないこと（BR-SEC-01）。
    """
    token = env.get(_TOKEN_ENV, "").strip()
    if token:
        return token
    return read_env_value(repo_root / ".env", _TOKEN_ENV)


def has_slack_token(repo_root: Path, env: Mapping[str, str]) -> bool:
    """Bot Token が解決できるかだけを返す（値を呼び出し側のフレームへ残さない, BR-SEC-01）。

    トークンを UI 側のローカル変数に束縛すると、そのフレームで未捕捉例外が起きたときに
    フレームワークのクラッシュ画面（locals ダンプ）へトークンが露出する（NFR-SEC-04）。
    事前チェックは真偽値だけを返し、値の解決は実際に投稿する直前まで遅らせる。
    """
    return bool(resolve_slack_token(repo_root, env))


# GFM→mrkdwn 変換用パターン（行頭見出し・太字・チェックボックス・箇条書き・水平線）。
# 見出しは行頭 `#`（インデント無し）のみ対象とする（インデント `#` はコード等なので除外）。
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*\S)\s*$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_CHECKBOX_UNCHECKED_RE = re.compile(r"^(\s*)[-*]\s+\[\s\]\s+")
_CHECKBOX_CHECKED_RE = re.compile(r"^(\s*)[-*]\s+\[[xX]\]\s+")
_BULLET_RE = re.compile(r"^(\s*)[-*]\s+")
# 親メッセージ整形用（トップ階層の箇条書き／インデントされた小項目）。
_TOP_BULLET_RE = re.compile(r"^[-*]\s+")
_SUB_BULLET_RE = re.compile(r"^\s+[-*]\s+")


def to_mrkdwn(markdown: str) -> str:
    """GitHub Markdown を Slack mrkdwn へ最小変換する（純粋）。

    Slack は標準 Markdown を解釈しないため、露出しがちな記法だけを寄せる:
      - 見出し `## X` → `*X*`（Slack に見出しが無いので太字化）。見出しの前には必ず空行を1行入れる
        （元の Markdown で見出し前の空行が省かれていても、セクション同士が詰まって見えないようにする）
      - 太字 `**X**` → `*X*`（Slack の太字は一重アスタリスク）
      - チェックボックス `- [ ]` → `☐ `（未完了）、`- [x]` → `☑ `（完了）、箇条書き `- ` → `• `
      - 水平線 `---` → 空行
    ` ``` ` フェンスで囲まれたコードブロックは変換せず素通しする（コマンド例の破壊を防ぐ）。
    完全な Markdown 解釈は行わない（リンクや表など稀な記法は素通し。YAGNI）。
    """
    out: list[str] = []
    in_fence = False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)  # フェンス行自体は素通し（Slack のコードブロックとして残す）
            continue
        if in_fence:
            out.append(line)  # コードブロック内は一切変換しない
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            if out and out[-1].strip():
                out.append("")  # 見出し前の空行を保証する（セクション間の詰まりを防ぐ）
            # 見出し内の `**bold**` は記号を外してから単一 * で包む（* の不均衡を防ぐ）。
            out.append(f"*{_BOLD_RE.sub(r'\1', heading.group(1))}*")
            continue
        if line.strip() == "---":
            out.append("")
            continue
        line = _CHECKBOX_CHECKED_RE.sub(r"\1☑ ", line)
        line = _CHECKBOX_UNCHECKED_RE.sub(r"\1☐ ", line)
        line = _BULLET_RE.sub(r"\1• ", line)
        out.append(_BOLD_RE.sub(r"*\1*", line))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 純粋ロジック（親メッセージ生成・本文分割）
# ---------------------------------------------------------------------------
_MEETING_NAME_RE = re.compile(r"^#\s+(.*\S)\s*$")
_DATETIME_FIELD_RE = re.compile(r"^-\s*日時:\s*(.+)$")
_PARTICIPANTS_FIELD_RE = re.compile(r"^-\s*参加者:\s*(.+)$")


def _extract_header(markdown: str) -> tuple[str | None, str | None, str | None]:
    """先頭ヘッダー（`# 会議名` / `- 日時: ...` / `- 参加者: ...`）を抽出する（純粋・BR-SUM-09）。

    最初の `## ` 見出しに到達したら打ち切る（本文セクションを誤って拾わないようにする）。
    """
    meeting_name: str | None = None
    meeting_datetime: str | None = None
    participants: str | None = None
    for line in markdown.splitlines():
        if line.startswith("## "):
            break
        if meeting_name is None:
            m = _MEETING_NAME_RE.match(line)
            if m:
                meeting_name = m.group(1)
                continue
        m = _DATETIME_FIELD_RE.match(line)
        if m:
            meeting_datetime = m.group(1).strip()
            continue
        m = _PARTICIPANTS_FIELD_RE.match(line)
        if m:
            participants = m.group(1).strip()
    return meeting_name, meeting_datetime, participants


def _body_after_header(markdown: str) -> str:
    """最初の `## ` 見出し以降を返す（純粋）。無ければ入力をそのまま返す。

    フォールバック表示（決定事項/ToDo が無い議事録）で、既に親の先頭に出したヘッダー
    （会議名・日時・参加者）を本文側で重複させないために使う。
    """
    lines = markdown.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("## "):
            return "\n".join(lines[i:])
    return markdown


def build_parent(session: str, minutes_md: str, *, limit: int = _CHUNK_LIMIT) -> str:
    """親メッセージを構造化して組み立てる（純粋・mrkdwn）。

    レイアウト: タイトル（会議名。無ければ日時ラベル）→ 日時 → 参加者 →
    `■ 決定事項`（番号付き）→ `■ 主なToDo`（箇条書き）→ 脚注。
    決定事項/ToDo は minutes.md の該当セクションから抽出する。両方無い議事録は先頭本文で代替。
    全文（詳細）はスレッド返信側が担うため、親は要約に絞る。
    """
    meeting_name, meeting_datetime, participants = _extract_header(minutes_md)
    title = meeting_name if meeting_name else f"{_title_label(session)} 議事録"
    parts: list[str] = [f"*{title}*"]
    if meeting_datetime:
        parts.append(f"日時: {meeting_datetime}")
    if participants:
        parts.append(f"参加者: {participants}")

    decisions = _format_numbered(_section_body(minutes_md, _DECISION_KEYWORD))
    if decisions:
        parts += ["", "*■ 決定事項*", *decisions]

    todos = _format_bullets(_section_body(minutes_md, _TODO_KEYWORD))
    if todos:
        parts += ["", "*■ 主なToDo*", *todos]

    if not decisions and not todos:
        # 見出しが無い議事録は先頭本文をそのまま mrkdwn 化して載せる（ヘッダー分は重複を避けて除く）。
        parts += ["", to_mrkdwn(_body_after_header(minutes_md).strip()[:_PARENT_FALLBACK_CHARS])]

    parts += ["", _PARENT_FOOTER]
    text = "\n".join(parts).strip()
    if len(text) > limit:
        cut = text[: limit - 1].rstrip()
        if cut.count("*") % 2:  # 太字 * の途中で切れた場合は最後の * 手前まで戻す
            cut = cut[: cut.rfind("*")].rstrip()
        text = cut + "…"
    return text


def _title_label(session: str) -> str:
    """セッションID `yyyyMMdd-HHmmss` を `yyyy-MM-dd HH:mm` へ。非該当はそのまま返す。"""
    m = re.match(r"^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})\d{2}$", session)
    if not m:
        return session
    y, mo, d, h, mi = m.groups()
    return f"{y}-{mo}-{d} {h}:{mi}"


def has_section(markdown: str, keyword: str) -> bool:
    """`## <keyword で始まる見出し>` セクションが存在するか（純粋、公開版・②議事録編集が使う）。

    `build_parent` の決定事項／ToDo 抽出（`_section_body`）と同じ前方一致判定を公開する。
    手編集で見出し文言が変わると `build_parent` が先頭本文フォールバックへ静かに落ちるため、
    保存後の検査でこの判定を再利用する。
    """
    return any(
        line.startswith("## ") and line[3:].strip().startswith(keyword) for line in markdown.splitlines()
    )


def _section_body(markdown: str, keyword: str) -> list[str]:
    """`## <keyword で始まる見出し>` セクションの本文行を返す（見出し行は除く・純粋）。

    最初に一致したセクションのみを対象とし、次の `#`/`##` 見出しで打ち切る。見出しテキストが
    keyword で「始まる」ことを条件とし、部分一致（例: `未決定事項` が `決定事項` に誤ヒット）を防ぐ。
    """
    body: list[str] | None = None
    for line in markdown.splitlines():
        is_header = line.startswith("## ") or line.startswith("# ")
        if body is not None and is_header:
            break  # 収集中に次見出しへ到達＝セクション終了
        if line.startswith("## ") and line[3:].strip().startswith(keyword):
            body = []  # 対象セクション開始（見出し行自体は含めない）
            continue
        if body is not None and not is_header:
            body.append(line)
    return body or []


_THREAD_DEDUP_NOTE = "_（決定事項・ToDo は上の要約メッセージをご覧ください）_"


def _strip_sections(markdown: str, keywords: tuple[str, ...]) -> str:
    """`keywords` のいずれかで始まる `## 見出し` セクションを、本文ごと除去する（純粋）。

    親メッセージに全文相当を出しているセクション（決定事項・ToDo）を、スレッド全文から
    重複除去するために使う（`_section_body` と同じ境界判定：keyword は前方一致）。
    """
    out: list[str] = []
    skipping = False
    for line in markdown.splitlines():
        is_boundary = line.startswith("## ") or line.startswith("# ")
        if is_boundary:
            skipping = line.startswith("## ") and any(line[3:].strip().startswith(k) for k in keywords)
            if skipping:
                continue
        if not skipping:
            out.append(line)
    return "\n".join(out)


def build_thread_body(minutes_md: str) -> str:
    """スレッド全文の元テキストを組み立てる（純粋）。

    決定事項・ToDo は親メッセージが全文相当を表示済みのため、スレッドからは除いて重複を避ける
    （除いた場合のみ、その旨の注記を先頭に添える）。
    """
    dedup_keywords = (_DECISION_KEYWORD, _TODO_KEYWORD)
    has_summary = any(_section_body(minutes_md, k) for k in dedup_keywords)
    stripped = _strip_sections(minutes_md, dedup_keywords)
    if has_summary:
        return f"{_THREAD_DEDUP_NOTE}\n\n{stripped}"
    return stripped


def _format_numbered(lines: list[str]) -> list[str]:
    """トップ階層の箇条書きを 1. 2. 3. に、インデント小項目を `•` に整形する（純粋）。"""
    out: list[str] = []
    number = 0
    for line in lines:
        if not line.strip() or line.strip() == "---":
            continue
        top = _TOP_BULLET_RE.match(line)
        if top:
            number += 1
            out.append(f"{number}. {_BOLD_RE.sub(r'*\1*', line[top.end() :])}")
            continue
        sub = _SUB_BULLET_RE.match(line)
        if sub:
            out.append(f"    • {_BOLD_RE.sub(r'*\1*', line[sub.end() :])}")
            continue
        out.append(_BOLD_RE.sub(r"*\1*", line.strip()))
    return out


def _format_bullets(lines: list[str]) -> list[str]:
    """チェックボックス（完了/未完了を区別）/箇条書きを整形する（純粋）。

    担当・期限・完了条件などのサブ項目（インデントされた `- `）は `    • ` として残す。
    トップ階層の項目が複数行にまたがっていても見分けられるよう、2件目以降の項目の前には
    空行を1行入れる（1行に詰まっていると見落とされるため）。
    """
    out: list[str] = []
    first_item = True
    for line in lines:
        if not line.strip() or line.strip() == "---":
            continue
        sub = _SUB_BULLET_RE.match(line)
        if sub:
            out.append(f"    • {_BOLD_RE.sub(r'*\1*', line[sub.end():])}")
            continue
        if _CHECKBOX_CHECKED_RE.match(line):
            marker, stripped = "☑ ", _CHECKBOX_CHECKED_RE.sub("", line)
        elif _CHECKBOX_UNCHECKED_RE.match(line):
            marker, stripped = "☐ ", _CHECKBOX_UNCHECKED_RE.sub("", line)
        else:
            marker, stripped = "• ", _BULLET_RE.sub("", line)
        if not first_item:
            out.append("")
        first_item = False
        out.append(f"{marker}{_BOLD_RE.sub(r'*\1*', stripped)}")
    return out


def split_body(minutes_md: str, *, limit: int = _CHUNK_LIMIT) -> list[str]:
    """minutes.md 全文を上限内チャンクへ行境界で分割する（純粋）。空なら空リスト。"""
    text = minutes_md.strip()
    if not text:
        return []
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for piece in _split_lines(text, limit):
        add = len(piece) + 1  # 改行分
        if size + add > limit and buf:
            chunks.append("\n".join(buf))
            buf, size = [], 0
        buf.append(piece)
        size += add
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def _split_lines(text: str, limit: int) -> list[str]:
    """行に分割し、上限超の行はさらに固定長で硬分割した断片列を返す（純粋）。"""
    pieces: list[str] = []
    for line in text.splitlines():
        pieces.extend(_hard_split(line, limit) if len(line) > limit else [line])
    return pieces


def _hard_split(line: str, limit: int) -> list[str]:
    """上限を超える 1 行を固定長で硬分割する。"""
    return [line[i : i + limit] for i in range(0, len(line), limit)]


# ---------------------------------------------------------------------------
# 投稿（HTTP は seam）
# ---------------------------------------------------------------------------
def post_minutes(
    session: str,
    minutes_md: str,
    channel: str,
    *,
    token: str,
    approved: bool,
    poster: Poster | None = None,
    emit: Callable[[str], None] = print,
    parent: str | None = None,
    sleep: Sleeper = time.sleep,
) -> int:
    """親（要約）→ スレッド（全文分割）の順に投稿する。全成功で 0。

    `approved` は呼び出し側が投稿前プレビュー＋人間承認（y/N 等）を得たことの明示フラグ。
    外部公開の承認ゲートを「呼び出し側の慣習」でなくこの境界で担保する（BR-SEC-01 の運用方針）:
    False のまま呼ぶと投稿せず ValueError で停止する。新しい呼び出し経路（バッチ再共有・
    別スクリプト等）が承認を素通りして実名 PII を外部送信する事故を防ぐため必須引数とする。
    `parent` を渡すと親メッセージの再組立を省く（呼び出し側がプレビューで生成済みの場合）。
    """
    if not approved:
        raise ValueError("Slack 投稿には呼び出し側での投稿前プレビュー＋人間承認 (approved=True) が必要です。")
    parent_text = parent if parent is not None else build_parent(session, minutes_md)
    parent_ts = post_message(token, channel, parent_text, poster=poster, sleep=sleep)
    if not parent_ts:
        # 親 ts が取れないとスレッド化できず、全文がチャンネル直下へ流出する。中止する。
        raise ValueError("Slack 親メッセージの ts を取得できず、全文スレッド投稿を中止しました。")
    # 決定事項・ToDo は親で表示済みのため除いてから mrkdwn へ変換し分割する
    # （分割前に変換＝`**` がチャンク境界で割れない）。
    chunks = split_body(to_mrkdwn(build_thread_body(minutes_md)))
    for index, chunk in enumerate(chunks):
        if index:
            sleep(_CHUNK_INTERVAL_SEC)  # 連投でレート制限に当たり、途中で失敗するのを避ける
        post_message(token, channel, chunk, thread_ts=parent_ts, poster=poster, sleep=sleep)
    emit(f"Slack へ投稿しました: channel={channel} / 親1 + スレッド{len(chunks)}")
    return 0


# Slack のエラーコードは短く、そのままでは運用時に次の一手が分からない。頻出のものに対処を添える。
# チャンネル未参加の扱いは公開/非公開で分かれる（非公開は存在を隠すため not_in_channel でなく
# channel_not_found が返る）ので、両方の可能性を並べる。
_ERROR_HINTS = {
    "channel_not_found": (
        "チャンネルIDが誤っているか、プライベートチャンネルに Bot が参加していません"
        "（対象チャンネルで /invite）。名前ではなく C で始まるIDを指定してください。"
    ),
    "not_in_channel": "Bot がチャンネルに参加していません（対象チャンネルで /invite）。",
    "is_archived": "アーカイブ済みのチャンネルには投稿できません。",
    "invalid_auth": "SLACK_BOT_TOKEN が無効です（再発行した直後なら .env の更新漏れを確認）。",
    "missing_scope": (
        "Bot の権限が足りません（投稿には chat:write。パブリックチャンネルへ未参加のまま"
        "投稿するなら chat:write.public）。"
    ),
    "ratelimited": "Slack のレート制限に掛かりました。少し待って再実行してください。",
}


def describe_error(code: str) -> str:
    """Slack のエラーコードに、分かっていれば対処を添えて返す（純粋）。

    対処は改行して独立した行にする。TUI のログは折り返さないため、1 行に詰めると
    肝心の対処がウィジェット幅で切れて読めなくなる（表示側は改行で行に割る）。
    """
    hint = _ERROR_HINTS.get(code)
    head = f"Slack API エラー: {code}"
    return f"{head}\n  → {hint}" if hint else head


def post_message(
    token: str,
    channel: str,
    text: str,
    *,
    thread_ts: str | None = None,
    poster: Poster | None = None,
    sleep: Sleeper = time.sleep,
) -> str:
    """1メッセージを投稿し、message ts（スレッド化に使う）を返す。ok:false は例外。

    `ratelimited` だけは Retry-After に従って有限回だけ再試行する。ここで即座に諦めると、
    全文スレッドが途中まで投稿された状態で失敗し、公開チャンネルに半端な記録が残る
    （しかも再実行は親メッセージから重複する）。それ以外のエラーは即座に停止する。
    """
    send = poster or _http_post
    payload: dict[str, Any] = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts

    for attempt in range(_MAX_RATELIMIT_RETRIES + 1):
        resp = send(token, payload)
        if resp.get("ok"):
            return str(resp.get("ts", ""))
        code = str(resp.get("error", "unknown"))
        if code != _RATELIMITED or attempt >= _MAX_RATELIMIT_RETRIES:
            raise ValueError(describe_error(code))
        sleep(_retry_after_sec(resp))
    # ループ内で必ず return / raise するため到達しない（網羅性のために置く）。
    raise ValueError(describe_error(_RATELIMITED))


def _retry_after_sec(resp: Mapping[str, Any]) -> float:
    """レート制限の待機秒を決める（Retry-After 優先・欠落/不正は既定値・上限で丸める・純粋）。"""
    try:
        seconds = float(resp.get(_RETRY_AFTER_KEY))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        seconds = _DEFAULT_RETRY_AFTER_SEC
    return min(max(seconds, 0.0), _MAX_RETRY_AFTER_SEC)


def _http_post(token: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """chat.postMessage を urllib で叩く（既定トランスポート）。トークンはログに出さない。"""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _SLACK_POST_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    status: int | None = None
    retry_after: str | None = None
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # **HTTPError は URLError の派生**。接続失敗より先に捕まえて本文を読む。Slack は 429 でも
        # JSON（ok:false / error:ratelimited）を返すため、接続失敗に丸めると原因も対処も失われ、
        # `_ERROR_HINTS["ratelimited"]` に到達できなくなる（Retry-After も捨ててしまう）。
        status = exc.code
        body = exc.read().decode("utf-8", "replace")
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
    except (urllib.error.URLError, TimeoutError) as exc:
        # 接続失敗（URLError）に加え、応答読取のタイムアウト（TimeoutError＝URLError 非派生）も捕捉。
        raise ValueError(f"Slack への接続に失敗しました: {exc}") from exc
    try:
        parsed: dict[str, Any] = json.loads(body)
    except json.JSONDecodeError as exc:
        detail = f"HTTP {status}: " if status is not None else ""
        raise ValueError(f"Slack 応答の解析に失敗しました（{detail}{exc}）") from exc
    if retry_after is not None:
        parsed.setdefault(_RETRY_AFTER_KEY, retry_after)  # ヘッダ由来の待機秒を seam 越しに渡す
    return parsed
