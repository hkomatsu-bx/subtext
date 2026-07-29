"""Slack 投稿（meeting slack / process の投稿段）のテスト。

HTTP トランスポートは `post` seam でモックし、ネットワークに触れない。Bot Token は
tmp のルート .env 経由で解決させ、実環境の環境変数に依存しない（monkeypatch で除去）。
"""

from __future__ import annotations

import json
import email.message
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from meeting import slack
from meeting.cli import main
from meeting.config import MeetingConfig

# conftest を直接参照する（詳細は test_runner.py の同 import の注記を参照）。
from conftest import write_manifest, write_out_file

_SESSION = "20260625-120156"
_MINUTES = (
    "# 議事録 20260625\n"
    "## 決定事項\n- A を承認\n\n"
    "## ToDo\n- 田中: 見積提出（7/31）\n\n"
    "## 論点・議論サマリ\n- 予算について議論\n"
)


def _make_poster(ts: str = "111.222"):
    """(ok:true, ts) を返すダミー poster と、呼び出し payload の記録を返す。"""
    calls: list[dict] = []

    def poster(token: str, payload):
        calls.append(dict(payload))
        return {"ok": True, "ts": ts}

    return poster, calls


# --- 純粋ロジック -------------------------------------------------------------


@pytest.mark.unit
def test_build_parent_extracts_decision_and_todo() -> None:
    parent = slack.build_parent(_SESSION, _MINUTES)
    assert "*■ 決定事項*" in parent
    assert "*■ 主なToDo*" in parent
    assert "A を承認" in parent
    # 論点セクションは親に含めない（要約に絞る）。
    assert "論点・議論サマリ" not in parent


@pytest.mark.unit
def test_build_parent_structures_with_numbering_and_footer() -> None:
    md = (
        "# 議事録\n"
        "## 決定事項\n- **A社対応**: 承認\n- B案を採用\n"
        "  - 補足の小項目\n---\n"
        "## ToDo\n- [ ] タスクX（担当: 自分）\n\n"
        "## 論点・議論サマリ\n### 1. 背景\n本文"
    )
    parent = slack.build_parent("20260728-113105", md)
    assert parent.splitlines()[0] == "*2026-07-28 11:31 議事録*"
    assert "1. *A社対応*: 承認" in parent  # トップ項目は番号付き＋太字変換
    assert "2. B案を採用" in parent
    assert "    • 補足の小項目" in parent  # 小項目は • インデント
    assert "• タスクX（担当: 自分）" in parent
    assert parent.rstrip().endswith(slack._PARENT_FOOTER)
    assert "論点" not in parent  # 論点は親に含めない
    assert "---" not in parent  # 水平線は除去
    assert "**" not in parent and "## " not in parent


@pytest.mark.unit
def test_build_parent_title_fallback_for_nonstandard_session() -> None:
    parent = slack.build_parent("custom-x", "## 決定事項\n- 決定A")
    assert parent.splitlines()[0] == "*custom-x 議事録*"


@pytest.mark.unit
def test_build_parent_fallbacks_to_head_when_no_sections() -> None:
    parent = slack.build_parent(_SESSION, "# タイトル\n本文だけで見出しなし")
    assert "本文だけで見出しなし" in parent


@pytest.mark.unit
def test_to_mrkdwn_converts_heading_bold_checkbox_and_hr() -> None:
    src = "## 決定事項\n- **重要**: 承認\n- [ ] タスクA\n---\n通常行"
    result = slack.to_mrkdwn(src)
    lines = result.splitlines()
    assert lines[0] == "*決定事項*"  # 見出し → 太字
    assert lines[1] == "• *重要*: 承認"  # 箇条書き + 太字（**→*）
    assert lines[2] == "• タスクA"  # チェックボックス → bullet
    assert lines[3] == ""  # 水平線 → 空行
    assert lines[4] == "通常行"  # 素通し
    assert "**" not in result  # 二重アスタリスクが残らない


@pytest.mark.unit
def test_build_parent_has_no_raw_markdown_markers() -> None:
    parent = slack.build_parent(_SESSION, _MINUTES)
    assert "## " not in parent  # 見出し記号が露出しない
    assert "**" not in parent  # 二重アスタリスクが残らない


@pytest.mark.unit
def test_split_body_single_chunk_for_short_text() -> None:
    assert slack.split_body("短い議事録") == ["短い議事録"]


@pytest.mark.unit
def test_split_body_splits_long_text_within_limit() -> None:
    long_md = "\n".join(f"line {i}" for i in range(2000))
    chunks = slack.split_body(long_md, limit=200)
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)


@pytest.mark.unit
def test_split_body_empty_returns_empty() -> None:
    assert slack.split_body("   ") == []


@pytest.mark.unit
def test_post_message_raises_on_not_ok() -> None:
    def poster(token, payload):
        return {"ok": False, "error": "channel_not_found"}

    with pytest.raises(ValueError, match="channel_not_found"):
        slack.post_message("xoxb-x", "C1", "hi", poster=poster)


@pytest.mark.unit
def test_post_minutes_posts_parent_then_threaded_body() -> None:
    poster, calls = _make_poster(ts="999.000")
    rc = slack.post_minutes(
        _SESSION, _MINUTES, "C1", token="xoxb-x", approved=True, poster=poster, emit=lambda _m: None
    )
    assert rc == 0
    # 1通目=親（thread_ts なし）、2通目以降=スレッド返信（thread_ts=親ts）。
    assert "thread_ts" not in calls[0]
    assert calls[0]["channel"] == "C1"
    assert len(calls) >= 2
    assert all(c.get("thread_ts") == "999.000" for c in calls[1:])


# --- トークン解決 -------------------------------------------------------------


@pytest.mark.unit
def test_resolve_token_prefers_environment(repo: Path) -> None:
    token = slack.resolve_slack_token(repo, {"SLACK_BOT_TOKEN": "xoxb-env"})
    assert token == "xoxb-env"


@pytest.mark.unit
def test_resolve_token_falls_back_to_dotenv(repo: Path) -> None:
    (repo / ".env").write_text('SLACK_BOT_TOKEN="xoxb-file"\n# comment\n', encoding="utf-8")
    token = slack.resolve_slack_token(repo, {})
    assert token == "xoxb-file"


@pytest.mark.unit
def test_resolve_token_none_when_absent(repo: Path) -> None:
    assert slack.resolve_slack_token(repo, {}) is None


# --- CLI（meeting slack） -----------------------------------------------------


@pytest.mark.integration
def test_slack_command_posts_with_confirmation(cfg: MeetingConfig, repo: Path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    (repo / ".env").write_text("SLACK_BOT_TOKEN=xoxb-file\n", encoding="utf-8")
    write_manifest(repo, _SESSION, self_sec=60.0, others_sec=60.0)
    write_out_file(repo, _SESSION, "minutes.md", _MINUTES)

    poster, calls = _make_poster()
    rc = main(["slack", _SESSION, "--channel", "C123"], cfg=cfg, ask=lambda _p: "y", post=poster)

    assert rc == 0
    assert calls[0]["channel"] == "C123"
    assert "決定事項" in calls[0]["text"]  # 親に要約
    assert len(calls) >= 2  # 全文はスレッド分割


@pytest.mark.integration
def test_slack_command_declined_does_not_post(cfg: MeetingConfig, repo: Path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    (repo / ".env").write_text("SLACK_BOT_TOKEN=xoxb-file\n", encoding="utf-8")
    write_manifest(repo, _SESSION, self_sec=60.0, others_sec=60.0)
    write_out_file(repo, _SESSION, "minutes.md", _MINUTES)

    poster, calls = _make_poster()
    rc = main(["slack", _SESSION, "--channel", "C123"], cfg=cfg, ask=lambda _p: "n", post=poster)

    assert rc == 0
    assert calls == []  # 未承認なら投稿しない


@pytest.mark.integration
def test_slack_command_missing_token_errors(cfg: MeetingConfig, repo: Path, monkeypatch) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    write_manifest(repo, _SESSION, self_sec=60.0, others_sec=60.0)
    write_out_file(repo, _SESSION, "minutes.md", _MINUTES)

    poster, calls = _make_poster()
    rc = main(["slack", _SESSION, "--channel", "C123"], cfg=cfg, ask=lambda _p: "y", post=poster)

    assert rc == 1
    assert calls == []


@pytest.mark.integration
def test_slack_command_no_minutes_errors(cfg: MeetingConfig, repo: Path, monkeypatch) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    (repo / ".env").write_text("SLACK_BOT_TOKEN=xoxb-file\n", encoding="utf-8")
    write_manifest(repo, _SESSION, self_sec=60.0, others_sec=60.0)  # minutes.md なし

    poster, calls = _make_poster()
    rc = main(["slack", _SESSION, "--channel", "C123"], cfg=cfg, ask=lambda _p: "y", post=poster)

    assert rc == 1
    assert calls == []


@pytest.mark.integration
def test_process_offers_slack_and_posts(cfg: MeetingConfig, repo: Path, monkeypatch, capsys) -> None:
    """process の最後に Slack 投稿が提案され、承認で投稿されることを結合で確認する。"""
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    (repo / ".env").write_text("SLACK_BOT_TOKEN=xoxb-file\n", encoding="utf-8")
    write_manifest(repo, _SESSION, self_sec=120.0, others_sec=120.0)

    state = {"n": 0}

    def run(cmd, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            write_out_file(
                repo,
                _SESSION,
                "speaker_names.json",
                json.dumps(
                    {
                        "sessionId": _SESSION,
                        "mappings": {"spk_0": "", "self": "自分"},
                        "unresolved": [],
                        "_clusters": [
                            {"label": "spk_0", "sampleUtterances": ["やあ"], "segmentCount": 2, "totalSec": 6.0}
                        ],
                    },
                    ensure_ascii=False,
                ),
            )
        else:
            write_out_file(repo, _SESSION, "final_transcript.json", json.dumps({"segments": [{"text": "x" * 1000}]}))
            write_out_file(repo, _SESSION, "minutes.md", _MINUTES)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    poster, calls = _make_poster()
    rc = main(["process", _SESSION, "--channel", "C123"], cfg=cfg, run=run, ask=lambda _p: "y", post=poster)

    assert rc == 0
    assert calls and calls[0]["channel"] == "C123"
    assert "決定事項" in calls[0]["text"]


# --- 回帰テスト（コードレビュー指摘の修正確認） -------------------------------


@pytest.mark.unit
def test_section_body_does_not_match_undecided_prefix() -> None:
    """`未決定事項` を `決定事項` セクションとして誤抽出しない（前方一致化）。"""
    md = "# 議事録\n## 未決定事項\n- 保留中の件\n"
    assert slack._section_body(md, "決定事項") == []
    assert "*■ 決定事項*" not in slack.build_parent(_SESSION, md)


@pytest.mark.unit
def test_to_mrkdwn_preserves_code_fence_and_indented_hash() -> None:
    """コードフェンス内は変換せず素通し、インデント `#` は見出し扱いしない。"""
    src = "```\n# コメント\n**そのまま**\n```\n    # インデント行"
    lines = slack.to_mrkdwn(src).splitlines()
    assert lines[0] == "```"
    assert lines[1] == "# コメント"  # フェンス内は不変
    assert lines[2] == "**そのまま**"  # 太字も不変
    assert lines[3] == "```"
    assert lines[4] == "    # インデント行"  # インデント # は見出し化しない


@pytest.mark.unit
def test_to_mrkdwn_heading_with_bold_is_balanced() -> None:
    """見出し内の `**bold**` でアスタリスクが不均衡にならない。"""
    assert slack.to_mrkdwn("## **重要**").splitlines()[0] == "*重要*"
    assert slack.to_mrkdwn("## **重要**な決定").splitlines()[0] == "*重要な決定*"


@pytest.mark.unit
def test_post_minutes_raises_when_parent_ts_missing() -> None:
    """親 ts が取れないときは全文の非スレッド流出を避けて停止する。"""

    def poster(token, payload):
        return {"ok": True}  # ts フィールド無し

    with pytest.raises(ValueError, match="ts"):
        slack.post_minutes(_SESSION, _MINUTES, "C1", token="x", approved=True, poster=poster, emit=lambda _m: None)


@pytest.mark.unit
def test_post_minutes_refuses_without_approval() -> None:
    """承認フラグ無し（approved=False）では投稿せず ValueError で停止する（境界での承認ゲート）。"""
    poster, calls = _make_poster()
    with pytest.raises(ValueError, match="承認"):
        slack.post_minutes(_SESSION, _MINUTES, "C1", token="x", approved=False, poster=poster, emit=lambda _m: None)
    assert calls == []


@pytest.mark.unit
def test_read_env_handles_inline_comment(repo: Path) -> None:
    (repo / ".env").write_text("SLACK_BOT_TOKEN=xoxb-123 # 本番用\n", encoding="utf-8")
    assert slack.resolve_slack_token(repo, {}) == "xoxb-123"


@pytest.mark.unit
def test_read_env_handles_export_prefix(repo: Path) -> None:
    (repo / ".env").write_text("export SLACK_BOT_TOKEN=xoxb-abc\n", encoding="utf-8")
    assert slack.resolve_slack_token(repo, {}) == "xoxb-abc"


@pytest.mark.unit
def test_build_parent_truncation_keeps_bold_balanced() -> None:
    """親メッセージ切り詰めが `*` の途中で切れない。"""
    md = "## 決定事項\n" + "\n".join(f"- **項目{i}**" for i in range(300))
    parent = slack.build_parent(_SESSION, md, limit=200)
    assert len(parent) <= 200
    assert parent.count("*") % 2 == 0


@pytest.mark.unit
def test_http_post_wraps_timeout(monkeypatch) -> None:
    """応答読取タイムアウト（TimeoutError）も ValueError に変換する。"""

    def fake_urlopen(*_a, **_k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(slack.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ValueError, match="接続に失敗"):
        slack._http_post("xoxb-x", {"channel": "C1", "text": "hi"})


def _http_error(code: int, body: str, headers: dict[str, str] | None = None):
    """urllib の HTTPError を投げる fake urlopen を返す（HTTPError は URLError の派生）。"""
    import io

    def fake_urlopen(*_a, **_k):
        raise urllib.error.HTTPError(
            url=slack._SLACK_POST_URL,
            code=code,
            msg="err",
            hdrs=_message(headers or {}),
            fp=io.BytesIO(body.encode("utf-8")),
        )

    return fake_urlopen


def _message(headers: dict[str, str]) -> email.message.Message:
    msg = email.message.Message()
    for key, value in headers.items():
        msg[key] = value
    return msg


@pytest.mark.unit
def test_http_post_reads_body_of_http_error(monkeypatch) -> None:
    """429 の本文（ok:false / error:ratelimited）と Retry-After を読み取ること。

    `HTTPError` は `URLError` の派生なので、接続失敗の except に先取りされると 429 が
    「接続に失敗しました」に化け、原因・対処（待って再実行）・Retry-After のすべてが失われる。
    """
    monkeypatch.setattr(
        slack.urllib.request,
        "urlopen",
        _http_error(429, '{"ok": false, "error": "ratelimited"}', {"Retry-After": "7"}),
    )

    resp = slack._http_post("xoxb-x", {"channel": "C1", "text": "hi"})

    assert resp["error"] == "ratelimited"
    assert slack._retry_after_sec(resp) == 7.0


@pytest.mark.unit
def test_http_post_reports_status_when_body_is_not_json(monkeypatch) -> None:
    """本文が JSON でない（プロキシの HTML 等）場合はステータスを添えて停止すること。"""
    monkeypatch.setattr(slack.urllib.request, "urlopen", _http_error(502, "<html>bad gateway</html>"))

    with pytest.raises(ValueError, match="HTTP 502"):
        slack._http_post("xoxb-x", {"channel": "C1", "text": "hi"})


@pytest.mark.unit
def test_post_message_retries_on_ratelimited_then_succeeds() -> None:
    """レート制限は Retry-After に従って再試行すること（半端なスレッドを残さない）。"""
    attempts: list[dict] = []
    slept: list[float] = []

    def poster(token, payload):
        attempts.append(dict(payload))
        if len(attempts) == 1:
            return {"ok": False, "error": "ratelimited", "retry_after": "3"}
        return {"ok": True, "ts": "1.0"}

    ts = slack.post_message("xoxb-x", "C1", "hi", poster=poster, sleep=slept.append)

    assert ts == "1.0"
    assert len(attempts) == 2
    assert slept == [3.0]


@pytest.mark.unit
def test_post_message_gives_up_with_actionable_hint_after_retries() -> None:
    """再試行を尽くしたら ratelimited の対処を添えて停止すること（従来は到達不能だった）。"""
    slept: list[float] = []

    def poster(token, payload):
        return {"ok": False, "error": "ratelimited"}

    with pytest.raises(ValueError, match="少し待って再実行"):
        slack.post_message("xoxb-x", "C1", "hi", poster=poster, sleep=slept.append)

    # Retry-After 欠落時は既定値で待ち、無限に再試行しない。
    assert slept == [slack._DEFAULT_RETRY_AFTER_SEC] * slack._MAX_RATELIMIT_RETRIES


@pytest.mark.unit
def test_post_message_does_not_retry_other_errors() -> None:
    """レート制限以外は即停止（無駄な待機と重複投稿を避ける）。"""
    attempts = 0

    def poster(token, payload):
        nonlocal attempts
        attempts += 1
        return {"ok": False, "error": "channel_not_found"}

    with pytest.raises(ValueError, match="channel_not_found"):
        slack.post_message("xoxb-x", "C1", "hi", poster=poster, sleep=lambda _s: None)

    assert attempts == 1


@pytest.mark.unit
def test_post_minutes_paces_thread_chunks() -> None:
    """全文スレッドは待機を挟んで連投すること（待機なしだとレート制限で途中失敗する）。"""
    poster, calls = _make_poster(ts="999.000")
    slept: list[float] = []

    long_minutes = "## 決定事項\n" + "\n".join(f"- 項目{i} " + "x" * 200 for i in range(60))
    slack.post_minutes(
        _SESSION,
        long_minutes,
        "C1",
        token="xoxb-x",
        approved=True,
        poster=poster,
        emit=lambda _m: None,
        sleep=slept.append,
    )

    thread_posts = len(calls) - 1
    assert thread_posts >= 2, "複数チャンクに分割されること（前提の確認）"
    assert slept == [slack._CHUNK_INTERVAL_SEC] * (thread_posts - 1)


@pytest.mark.integration
def test_slack_preview_shows_full_body(cfg: MeetingConfig, repo: Path, monkeypatch, capsys) -> None:
    """プレビューに親要約だけでなくスレッド全文も表示される（承認ゲートが全体をカバー）。"""
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    (repo / ".env").write_text("SLACK_BOT_TOKEN=xoxb-file\n", encoding="utf-8")
    write_manifest(repo, _SESSION, self_sec=60.0, others_sec=60.0)
    write_out_file(repo, _SESSION, "minutes.md", _MINUTES)

    poster, calls = _make_poster()
    rc = main(["slack", _SESSION, "--channel", "C1"], cfg=cfg, ask=lambda _p: "n", post=poster)

    out = capsys.readouterr().out
    assert rc == 0 and calls == []  # 未承認なので投稿しない
    assert "スレッド全文" in out
    assert "予算について議論" in out  # 親に含まれない論点セクションがプレビューに出る


@pytest.mark.integration
def test_process_slack_failure_does_not_fail_run(cfg: MeetingConfig, repo: Path, monkeypatch, capsys) -> None:
    """任意オファーの Slack 投稿が失敗しても process 全体は成功のまま（議事録は生成済み）。"""
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    (repo / ".env").write_text("SLACK_BOT_TOKEN=xoxb-file\n", encoding="utf-8")
    write_manifest(repo, _SESSION, self_sec=120.0, others_sec=120.0)

    state = {"n": 0}

    def run(cmd, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            write_out_file(
                repo,
                _SESSION,
                "speaker_names.json",
                json.dumps(
                    {
                        "sessionId": _SESSION,
                        "mappings": {"spk_0": "", "self": "自分"},
                        "unresolved": [],
                        "_clusters": [],
                    },
                    ensure_ascii=False,
                ),
            )
        else:
            write_out_file(repo, _SESSION, "final_transcript.json", json.dumps({"segments": [{"text": "x" * 1000}]}))
            write_out_file(repo, _SESSION, "minutes.md", _MINUTES)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    def failing_poster(token, payload):
        return {"ok": False, "error": "channel_not_found"}

    rc = main(["process", _SESSION, "--channel", "C123"], cfg=cfg, run=run, ask=lambda _p: "y", post=failing_poster)

    assert rc == 0  # 投稿失敗でも成功終了
    assert "Slack 投稿に失敗" in capsys.readouterr().err


@pytest.mark.unit
def test_describe_error_adds_actionable_hint_on_its_own_line() -> None:
    """頻出コードには対処を添える。対処は独立した行にする（表示幅で切れないため）。"""
    lines = slack.describe_error("channel_not_found").splitlines()
    assert len(lines) == 2
    assert lines[0] == "Slack API エラー: channel_not_found"
    assert "/invite" in lines[1]


@pytest.mark.unit
def test_describe_error_passes_through_unknown_code() -> None:
    """未知のコードは素通しする（勝手な推測を足さない）。"""
    assert slack.describe_error("some_new_code") == "Slack API エラー: some_new_code"


@pytest.mark.unit
def test_post_message_error_surfaces_hint() -> None:
    def failing_poster(_token, _payload):
        return {"ok": False, "error": "channel_not_found"}

    with pytest.raises(ValueError) as exc:
        slack.post_message("xoxb-x", "C123", "本文", poster=failing_poster)
    assert "/invite" in str(exc.value)
