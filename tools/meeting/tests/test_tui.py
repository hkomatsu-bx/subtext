"""tui.py の Textual Pilot によるスモーク/結合テスト。

長時間処理（subprocess/Slack HTTP）は既存 CLI テストと同じ seam（run/post）でモックし、
実 subprocess・実ネットワークには一切触れない。ワーカー完了待ちは `app.workers.wait_for_complete()`
を使う（`pilot.pause()` だけではスレッドワーカーの完了を待てないため）。
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from textual.content import Content
from textual.pilot import Pilot
from textual.widgets import Button, DataTable, Input, Log, Static, TextArea

from meeting import ledger, slack
from meeting.config import MeetingConfig
from meeting.tui import MeetingApp
from meeting.tui_view import MIC_RED, MeetingCommands, strip_path_quotes

# conftest を直接参照する（詳細は test_runner.py の同 import の注記を参照）。
from conftest import write_manifest, write_out_file, write_pipeline_outputs

_SESSION = "20260625-120156"


# 起動時の AWS 疎通確認（TUI が on_mount で必ず投げる）への定型応答。実 AWS へは出ない。
_AUTH_OK_STDOUT = "authProfile=default\nauthRegion=ap-northeast-1\nauthStatus=ok\n"


def _auth_response(cmd):
    """`--check-auth` の呼び出しなら成功応答を返す（それ以外は None）。

    各フェイク runner はこれを**最初に**返す。そうしないと起動時チェックが、各テストが数えている
    呼び出し回数・引数・副作用（`side_effect`）に混ざる。
    """
    if "--check-auth" in cmd:
        return SimpleNamespace(returncode=0, stdout=_AUTH_OK_STDOUT, stderr="")
    return None


def _ok_runner(side_effect=None):
    def _run(cmd, **kwargs):
        if (auth := _auth_response(cmd)) is not None:
            return auth
        if side_effect is not None:
            side_effect(cmd, kwargs)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    return _run


def _run_async(coro):
    return asyncio.run(coro)


class _FakeProcess:
    """subprocess.Popen 互換の最小フェイク。行のイテレーションと poll/wait/kill を提供する。"""

    def __init__(self, lines: list[str], *, exit_code: int = 0) -> None:
        self.stdout = iter(lines)
        self._returncode: int | None = None
        self._exit_code = exit_code
        self.killed = False
        self.wait_calls: list[float | None] = []

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        self._returncode = self._exit_code
        return self._exit_code

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9


def _fake_popen(lines: list[str], *, exit_code: int = 0):
    calls: list[tuple[list[str], dict]] = []

    def factory(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _FakeProcess(lines, exit_code=exit_code)

    return factory, calls


async def _run_palette_command(app: MeetingApp, label_contains: str) -> None:
    """コマンドパレット（MeetingCommands）から、ラベルに label_contains を含むコマンドを実行する。

    実際の palette UI 操作（ctrl+p→検索→Enter）は経由せず、Provider を直接叩く
    （Provider.discover()/search() は Textual 標準の呼び出し規約であり、ここではその
    戻り値の command を呼ぶだけなので実際の palette 経由と等価）。
    """
    provider = MeetingCommands(app.screen)
    hits = [hit async for hit in provider.discover()]
    hit = next(h for h in hits if label_contains in str(h.display))
    hit.command()


def _make_dist_exe(cfg: MeetingConfig) -> Path:
    """publish 済み recorder exe を模す（needs_recorder_build を False にしてビルドを省く）。"""
    cfg.recorder_dist_exe.parent.mkdir(parents=True, exist_ok=True)
    cfg.recorder_dist_exe.write_text("exe", encoding="utf-8")
    return cfg.recorder_dist_exe


@pytest.mark.integration
def test_mount_lists_sessions(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=120.0, others_sec=118.0)

    async def scenario() -> list[str]:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            table = app.query_one("#table")
            # 列0は通し番号（#）。セッションIDは列1。
            return [str(table.get_cell_at((row, 1))) for row in range(table.row_count)]

    rows = _run_async(scenario())
    assert rows == [_SESSION]


@pytest.mark.integration
def test_layout_does_not_overlap_header_on_large_terminal(cfg: MeetingConfig) -> None:
    """回帰防止: アクション行(Horizontal)が height:1fr のまま残ると、80x24 の既定サイズでは
    偶然目立たなくても、実ターミナルの大きな画面ではアクション欄が全画面に伸びてタイトルバーと
    重なり、DataTable/Log が画面外に押し出される（本テスト追加のきっかけとなった不具合）。
    """

    async def scenario() -> tuple[str, bool]:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(172, 55)) as pilot:
            await pilot.pause()
            widget, _ = app.get_widget_at(5, 0)
            top_widget_type = type(widget).__name__
            clicked = await pilot.click("#refresh")
            return top_widget_type, clicked

    top_widget_type, clicked = _run_async(scenario())
    assert "Logo" in top_widget_type
    assert clicked


@pytest.mark.integration
def test_record_toggle_button_stops_recording_when_already_recording(cfg: MeetingConfig) -> None:
    """録音中に統合ボタン（#record-toggle）を押すと停止（stop-file 作成）になること。"""

    async def scenario() -> None:
        app = MeetingApp(cfg, run=_ok_runner())
        app._recorder._process = _FakeProcess(["x"])  # poll() が None のため録音中扱い
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#record-toggle")
            await pilot.pause()

    _run_async(scenario())
    assert cfg.stop_file.is_file()


@pytest.mark.integration
def test_minutes_claude_command_runs_pipeline_and_records_ledger(cfg: MeetingConfig, repo: Path) -> None:
    """[議事録(Claude)] ボタンは廃止済み。コマンドパレットの「議事録を生成 (Claude)」から呼ぶ。"""
    write_manifest(repo, _SESSION, self_sec=120.0, others_sec=120.0)

    def side_effect(cmd, kwargs):
        write_pipeline_outputs(repo, _SESSION, minutes=False)  # Claude 経路は minutes.md を作らない

    async def scenario() -> None:
        app = MeetingApp(cfg, run=_ok_runner(side_effect))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await _run_palette_command(app, "議事録を生成")
            await app.workers.wait_for_complete()
            await pilot.pause()

    _run_async(scenario())

    entries = ledger.load(cfg.ledger_path)
    assert {"transcribe", "claude"} == {e.stage for e in entries}


@pytest.mark.integration
def test_minutes_button_skips_when_already_done(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    write_out_file(repo, _SESSION, "final_transcript.json", "{}")
    write_out_file(repo, _SESSION, "minutes.md", "# 議事録")

    calls: list[list[str]] = []

    async def scenario() -> None:
        app = MeetingApp(cfg, run=_ok_runner(lambda cmd, kwargs: calls.append(cmd)))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.click("#minutes-bedrock")
            await pilot.pause()

    _run_async(scenario())
    assert calls == []  # 議事録生成済みなのでパイプラインは起動しない。


@pytest.mark.integration
def test_minutes_button_on_vtt_session_errors_without_crashing(cfg: MeetingConfig, repo: Path) -> None:
    """取込由来（FR-17。manifest 無し）で入力 VTT パスが不明なセッションに「議事録(Bedrock)」
    ボタンを押すと、アプリがクラッシュせず actionable なログで止まること（実クラッシュの再発防止）。

    録音由来専用の pipeline.run_minutes_pipeline は manifest.json を前提にしており、
    ガード無しで呼ぶと runner.estimate_transcribe → read_manifest が FileNotFoundError を
    送出してワーカーが例外終了し、Textual のデフォルト挙動でアプリ全体が落ちていた。

    入力 VTT パスを保持している場合は `--mode vtt` で再開する（別テストで検証）。
    """
    write_out_file(repo, "meeting-2026-06-01", "speaker_names.json")

    calls: list[list[str]] = []

    async def scenario() -> str:
        app = MeetingApp(cfg, run=_ok_runner(lambda cmd, kwargs: calls.append(cmd)))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = "meeting-2026-06-01"
            await pilot.click("#minutes-bedrock")
            await pilot.pause()
            return "\n".join(app.query_one("#log", Log).lines)

    text = _run_async(scenario())
    assert calls == []  # パイプラインは起動しない（manifest 無しのため）。
    assert "取込（VTT/mp4）由来" in text


@pytest.mark.integration
def test_slack_button_posts_after_confirmation(cfg: MeetingConfig, repo: Path) -> None:
    """Slack投稿モーダルは既定チャンネル（meeting.toml）が入力欄に入った状態で開く。"""
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    write_out_file(repo, _SESSION, "minutes.md", "## 決定事項\n- 進める\n")
    (cfg.repo_root / ".env").write_text("SLACK_BOT_TOKEN=xoxb-test\n", encoding="utf-8")
    cfg_with_channel = replace(cfg, slack_default_channel="C123")

    posted: list[dict] = []

    def fake_poster(token, payload):
        posted.append(dict(payload))
        return {"ok": True, "ts": "123.456"}

    async def scenario() -> None:
        app = MeetingApp(cfg_with_channel, run=_ok_runner(), post=fake_poster)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.click("#slack")
            await pilot.pause()
            await pilot.click("#ok")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()

    _run_async(scenario())
    assert posted, "Slack へ投稿されていること"
    assert posted[0]["channel"] == "C123"


@pytest.mark.integration
def test_slack_channel_input_overrides_toml_default(cfg: MeetingConfig, repo: Path) -> None:
    """モーダル内のチャンネル入力欄を書き換えれば、meeting.toml の既定ではなくそちらへ投稿される。"""
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    write_out_file(repo, _SESSION, "minutes.md", "## 決定事項\n- 進める\n")
    (cfg.repo_root / ".env").write_text("SLACK_BOT_TOKEN=xoxb-test\n", encoding="utf-8")
    cfg_with_channel = replace(cfg, slack_default_channel="C-DEFAULT")

    posted: list[dict] = []

    def fake_poster(token, payload):
        posted.append(dict(payload))
        return {"ok": True, "ts": "123.456"}

    async def scenario() -> None:
        app = MeetingApp(cfg_with_channel, run=_ok_runner(), post=fake_poster)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.click("#slack")
            await pilot.pause()
            app.screen.query_one("#channel-input", Input).value = "C-OVERRIDE"
            await pilot.click("#ok")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()

    _run_async(scenario())
    assert posted
    assert all(p["channel"] == "C-OVERRIDE" for p in posted)


@pytest.mark.integration
def test_slack_button_empty_channel_shows_error(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    write_out_file(repo, _SESSION, "minutes.md", "## 決定事項\n- 進める\n")
    (cfg.repo_root / ".env").write_text("SLACK_BOT_TOKEN=xoxb-test\n", encoding="utf-8")
    cfg_no_channel = replace(cfg, slack_default_channel="")

    async def scenario() -> str:
        app = MeetingApp(cfg_no_channel, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.click("#slack")
            await pilot.pause()
            await pilot.click("#ok")  # チャンネル欄は空のまま確定
            await pilot.pause()
            return "\n".join(app.query_one("#log", Log).lines)

    text = _run_async(scenario())
    assert "未入力" in text


@pytest.mark.integration
def test_slack_api_error_logs_and_keeps_app_running(cfg: MeetingConfig, repo: Path) -> None:
    """Slack API がエラーを返しても TUI は落ちず、1 行のログにして起動を続ける。

    Textual の既定（exit_on_error=True）ではワーカー内の例外がアプリを落とし、クラッシュ画面が
    フレームの locals をダンプする。そこに Slack Bot Token が載るため、落とさないことが
    そのままトークン非開示の担保になる（BR-SEC-01・NFR-SEC-04）。
    """
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    write_out_file(repo, _SESSION, "minutes.md", "## 決定事項\n- 進める\n")
    token = "xoxb-secret-should-never-surface"
    (cfg.repo_root / ".env").write_text(f"SLACK_BOT_TOKEN={token}\n", encoding="utf-8")
    cfg_with_channel = replace(cfg, slack_default_channel="does-not-exist")

    def failing_poster(_token, _payload):
        return {"ok": False, "error": "channel_not_found"}

    async def scenario() -> tuple[str, bool]:
        app = MeetingApp(cfg_with_channel, run=_ok_runner(), post=failing_poster)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.click("#slack")
            await pilot.pause()
            await pilot.click("#ok")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            return "\n".join(app.query_one("#log", Log).lines), app.is_running

    text, still_running = _run_async(scenario())
    assert still_running, "API エラーで TUI が落ちないこと"
    assert "channel_not_found" in text, "失敗の理由がログに出ること"
    assert "/invite" in text, "対処が独立した行として読めること（Log は折り返さない）"
    assert token not in text, "Bot Token をログへ出さないこと（BR-SEC-01）"


@pytest.mark.integration
def test_slack_non_utf8_minutes_logs_error_without_crashing(cfg: MeetingConfig, repo: Path) -> None:
    """UTF-8 でない minutes.md でも TUI は落ちず、対処付きの 1 行エラーにする。

    `UnicodeDecodeError` は `OSError` ではない（ValueError 系）ため except を OSError に絞ると
    捕り落とし、メインスレッドの未捕捉例外＝クラッシュ画面（各フレームの locals ダンプ）になる。
    そこに Slack Bot Token が載るため、落とさないこと自体がトークン非開示の担保になる
    （BR-SEC-01・NFR-SEC-04）。PowerShell の Set-Content や旧エディタで保存し直すと cp932 になり、
    投稿前に議事録を手直しする運用では日常的に起こり得る。
    """
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    out_dir = repo / "data" / "out" / _SESSION
    out_dir.mkdir(parents=True, exist_ok=True)
    # cp932 の「決定事項」は UTF-8 として不正なバイト列。
    (out_dir / "minutes.md").write_bytes("## 決定事項\n- 進める\n".encode("cp932"))
    (cfg.repo_root / ".env").write_text("SLACK_BOT_TOKEN=xoxb-test\n", encoding="utf-8")

    posted: list[dict] = []

    def fake_poster(token, payload):
        posted.append(dict(payload))
        return {"ok": True, "ts": "1.2"}

    async def scenario() -> tuple[str, bool]:
        app = MeetingApp(cfg, run=_ok_runner(), post=fake_poster)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.click("#slack")
            await pilot.pause()
            return "\n".join(app.query_one("#log", Log).lines), app.is_running

    text, still_running = _run_async(scenario())
    assert still_running, "非 UTF-8 の minutes.md で TUI が落ちないこと"
    assert "UTF-8" in text, "UTF-8 で保存し直す、という対処が読めること"
    assert not posted, "読み込めない議事録を投稿しないこと"


@pytest.mark.integration
def test_append_log_splits_multiline_into_separate_lines(cfg: MeetingConfig) -> None:
    """改行を含むメッセージは行ごとに書く。

    `Log` は折り返さないため、1 行に詰めるとウィジェット幅から先が横スクロールしないと
    読めない。エラーの対処のように「後半こそ読ませたい」文言が切れるのを避ける。
    """

    async def scenario() -> list[str]:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app.log_line("1行目\n  → 2行目")
            await pilot.pause()
            return list(app.query_one("#log", Log).lines)

    lines = _run_async(scenario())
    assert "1行目" in lines
    assert "  → 2行目" in lines


@pytest.mark.integration
def test_slack_button_cancelled_does_not_post(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    write_out_file(repo, _SESSION, "minutes.md", "## 決定事項\n- 進める\n")
    (cfg.repo_root / ".env").write_text("SLACK_BOT_TOKEN=xoxb-test\n", encoding="utf-8")
    cfg_with_channel = replace(cfg, slack_default_channel="C123")

    posted: list[dict] = []

    def fake_poster(token, payload):
        posted.append(dict(payload))
        return {"ok": True, "ts": "123.456"}

    async def scenario() -> None:
        app = MeetingApp(cfg_with_channel, run=_ok_runner(), post=fake_poster)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.click("#slack")
            await pilot.pause()
            await pilot.click("#cancel")
            await pilot.pause()

    _run_async(scenario())
    assert posted == []


@pytest.mark.unit
def test_current_session_returns_none_when_unselected(cfg: MeetingConfig) -> None:
    app = MeetingApp(cfg, run=_ok_runner())
    assert app._current_session() is None


# --- 録音開始（Popen 非ブロッキング起動） --------------------------------------


@pytest.mark.integration
def test_record_toggle_button_streams_output_via_popen(cfg: MeetingConfig) -> None:
    _make_dist_exe(cfg)
    popen_factory, calls = _fake_popen(["sessionId=20260101-000000", "Saved to X (status=complete)"])

    async def scenario() -> str:
        app = MeetingApp(cfg, run=_ok_runner(), popen=popen_factory)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#record-toggle")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            return "\n".join(app.query_one("#log", Log).lines)

    text = _run_async(scenario())
    assert calls, "Popen が呼ばれていること"
    assert "sessionId=20260101-000000" in text
    assert "録音が終了しました" in text


@pytest.mark.integration
def test_record_worker_reports_abnormal_exit_code(cfg: MeetingConfig) -> None:
    """recorder が異常終了コード（1=録音中の致命的異常/部分保存）で終わると、正常終了と
    区別して警告を出すこと（CLI の returncode 判定と挙動を揃える）。"""
    _make_dist_exe(cfg)
    popen_factory, calls = _fake_popen(["sessionId=X", "Fatal error"], exit_code=1)

    async def scenario() -> str:
        app = MeetingApp(cfg, run=_ok_runner(), popen=popen_factory)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#record-toggle")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            return "\n".join(app.query_one("#log", Log).lines)

    text = _run_async(scenario())
    assert calls, "Popen が呼ばれていること"
    assert "異常終了" in text and "exit=1" in text
    assert "録音が終了しました。" not in text  # 正常終了メッセージは出さない


@pytest.mark.integration
def test_record_toggle_button_rejects_invalid_minutes(cfg: MeetingConfig) -> None:
    popen_factory, calls = _fake_popen([])

    async def scenario() -> str:
        app = MeetingApp(cfg, run=_ok_runner(), popen=popen_factory)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app.query_one("#record-minutes", Input).value = "abc"
            await pilot.click("#record-toggle")
            await pilot.pause()
            return "\n".join(app.query_one("#log", Log).lines)

    text = _run_async(scenario())
    assert calls == []
    assert "不正" in text


@pytest.mark.integration
def test_start_record_guards_against_concurrent_start(cfg: MeetingConfig) -> None:
    """`_start_record()` 自体のガード（録音中の二重開始防止）を直接確認する。

    統合ボタン（#record-toggle）は録音中なら常に停止側へ分岐するため、ボタン経由では
    この分岐に到達しない。ガードそのものは防御的に残しているため、メソッド直呼びで検証する。
    """
    popen_factory, calls = _fake_popen([])

    async def scenario() -> str:
        app = MeetingApp(cfg, run=_ok_runner(), popen=popen_factory)
        app._recorder._process = _FakeProcess(["x"])  # poll() が None のため実行中扱い
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._start_record()
            await pilot.pause()
            return "\n".join(app.query_one("#log", Log).lines)

    text = _run_async(scenario())
    assert calls == []
    assert "既に録音中" in text


@pytest.mark.unit
def test_on_unmount_stops_running_record_process(cfg: MeetingConfig) -> None:
    process = _FakeProcess(["x"])
    app = MeetingApp(cfg, run=_ok_runner())
    app._recorder._process = process

    asyncio.run(app.on_unmount())

    assert cfg.stop_file.is_file()
    assert process.wait_calls, "graceful 停止のため wait が呼ばれていること"


@pytest.mark.unit
def test_on_unmount_kills_process_if_it_does_not_stop_gracefully(cfg: MeetingConfig) -> None:
    class _StubbornProcess(_FakeProcess):
        def wait(self, timeout: float | None = None) -> int:
            self.wait_calls.append(timeout)
            if not self.killed:
                raise subprocess.TimeoutExpired(cmd=["x"], timeout=timeout or 0)
            self._returncode = -9
            return -9

    process = _StubbornProcess(["x"])
    app = MeetingApp(cfg, run=_ok_runner())
    app._recorder._process = process

    asyncio.run(app.on_unmount())

    assert process.killed is True


@pytest.mark.unit
def test_on_unmount_noop_when_no_process(cfg: MeetingConfig) -> None:
    app = MeetingApp(cfg, run=_ok_runner())
    asyncio.run(app.on_unmount())  # 例外を投げずに戻ること
    assert not cfg.stop_file.exists()


# --- VTTインポート（FR-17） ------------------------------------------------------


@pytest.mark.unit
def test_strip_path_quotes_removes_matching_double_quotes() -> None:
    # Windows の「パスのコピー」（Shift+右クリック）はパスを "..." で囲んで貼り付けられる。
    assert strip_path_quotes('"C:\\path\\meeting.vtt"') == "C:\\path\\meeting.vtt"


@pytest.mark.unit
def test_strip_path_quotes_removes_matching_single_quotes() -> None:
    assert strip_path_quotes("'meeting.vtt'") == "meeting.vtt"


@pytest.mark.unit
def test_strip_path_quotes_leaves_unquoted_path_untouched() -> None:
    assert strip_path_quotes("meeting.vtt") == "meeting.vtt"


@pytest.mark.unit
def test_strip_path_quotes_leaves_unmatched_quote_untouched() -> None:
    # 開きクォートのみ（貼り付けミス等）は誤って中身を壊さないよう素通しする。
    assert strip_path_quotes('"meeting.vtt') == '"meeting.vtt'


@pytest.mark.integration
def test_import_vtt_button_accepts_quoted_path_from_windows_copy_as_path(
    cfg: MeetingConfig, repo: Path
) -> None:
    """Windows の「パスのコピー」で得た `"..."` 付きパスをそのまま貼っても取り込めること。"""
    vtt_path = repo / "meeting-2026-06-01.vtt"
    vtt_path.write_text("WEBVTT\n", encoding="utf-8")

    async def scenario() -> str:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#import-vtt")
            await pilot.pause()
            app.screen.query_one("#path-input", Input).value = f'"{vtt_path}"'
            await pilot.click("#ok")
            await pilot.pause()
            return "\n".join(app.query_one("#log", Log).lines)

    text = _run_async(scenario())
    assert "見つかりません" not in text


@pytest.mark.integration
def test_import_vtt_button_runs_pipeline_and_selects_session(cfg: MeetingConfig, repo: Path) -> None:
    vtt_path = repo / "meeting-2026-06-01.vtt"
    vtt_path.write_text("WEBVTT\n", encoding="utf-8")

    def side_effect(cmd, kwargs):
        write_pipeline_outputs(repo, "meeting-2026-06-01", chars=500, raw=False)

    async def scenario() -> tuple[str | None, str]:
        app = MeetingApp(cfg, run=_ok_runner(side_effect))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#import-vtt")
            await pilot.pause()
            app.screen.query_one("#path-input", Input).value = str(vtt_path)
            await pilot.click("#ok")
            await app.workers.wait_for_complete()
            await pilot.pause()
            return app._selected, "\n".join(app.query_one("#log", Log).lines)

    selected, text = _run_async(scenario())
    assert selected == "meeting-2026-06-01"
    assert "VTTインポート" in text
    entries = ledger.load(cfg.ledger_path)
    assert {e.stage for e in entries} == {"bedrock"}


@pytest.mark.integration
def test_import_vtt_claude_command_runs_pipeline(cfg: MeetingConfig, repo: Path) -> None:
    """[取込(Claude)] ボタンは廃止済み。コマンドパレットの「取込 VTT/mp4 (Claude)」から呼ぶ。"""
    vtt_path = repo / "meeting-2026-06-01.vtt"
    vtt_path.write_text("WEBVTT\n", encoding="utf-8")

    def side_effect(cmd, kwargs):
        write_out_file(
            repo, "meeting-2026-06-01", "final_transcript.json", json.dumps({"segments": [{"text": "x" * 500}]})
        )

    async def scenario() -> str | None:
        app = MeetingApp(cfg, run=_ok_runner(side_effect))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await _run_palette_command(app, "取込 VTT/mp4")
            await pilot.pause()
            app.screen.query_one("#path-input", Input).value = str(vtt_path)
            await pilot.click("#ok")
            await app.workers.wait_for_complete()
            await pilot.pause()
            return app._selected

    selected = _run_async(scenario())
    assert selected == "meeting-2026-06-01"
    entries = ledger.load(cfg.ledger_path)
    assert {e.stage for e in entries} == {"claude"}


@pytest.mark.integration
def test_import_vtt_button_missing_file_shows_error(cfg: MeetingConfig, repo: Path) -> None:
    async def scenario() -> str:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#import-vtt")
            await pilot.pause()
            app.screen.query_one("#path-input", Input).value = str(repo / "nope.vtt")
            await pilot.click("#ok")
            await pilot.pause()
            return "\n".join(app.query_one("#log", Log).lines)

    text = _run_async(scenario())
    assert "見つかりません" in text


@pytest.mark.integration
def test_import_vtt_button_empty_path_shows_error(cfg: MeetingConfig) -> None:
    async def scenario() -> str:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#import-vtt")
            await pilot.pause()
            await pilot.click("#ok")  # パス欄は空のまま確定
            await pilot.pause()
            return "\n".join(app.query_one("#log", Log).lines)

    text = _run_async(scenario())
    assert "未入力" in text


@pytest.mark.integration
def test_import_vtt_button_cancelled_shows_no_error(cfg: MeetingConfig) -> None:
    async def scenario() -> str:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#import-vtt")
            await pilot.pause()
            await pilot.click("#cancel")
            await pilot.pause()
            return "\n".join(app.query_one("#log", Log).lines)

    text = _run_async(scenario())
    assert "中止" in text
    assert "未入力" not in text


# --- セッション削除 ---------------------------------------------------------------


@pytest.mark.integration
def test_delete_session_button_removes_after_confirmation(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    write_out_file(repo, _SESSION, "minutes.md", "# 議事録")

    async def scenario() -> None:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.click("#delete-session")
            await pilot.pause()
            await pilot.click("#yes")
            await pilot.pause()

    _run_async(scenario())
    assert not cfg.session_recording_dir(_SESSION).is_dir()
    assert not cfg.session_out_dir(_SESSION).is_dir()


@pytest.mark.integration
def test_delete_session_button_cancelled_keeps_files(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)

    async def scenario() -> None:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.click("#delete-session")
            await pilot.pause()
            await pilot.click("#no")
            await pilot.pause()

    _run_async(scenario())
    assert cfg.session_recording_dir(_SESSION).is_dir()


@pytest.mark.integration
def test_delete_confirmation_button_is_labelled_for_deletion(cfg: MeetingConfig, repo: Path) -> None:
    """削除の承認ボタンが削除の文言であること（Slack 承認からの転用で文言が残らないように）。

    このモーダルは復元不能な rmtree（録音 WAV の唯一のコピーを含む）の唯一のゲート。ボタンが
    「投稿する (y)」に見えると、Slack 投稿を繰り返した操作者が反射的に y を押してセッションを失う。
    """
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)

    async def scenario() -> tuple[str, str]:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.click("#delete-session")
            await pilot.pause()
            return (
                str(app.screen.query_one("#yes", Button).label),
                app.screen.query_one("#confirm-body", Static).visual.plain,
            )

    label, body = _run_async(scenario())
    assert "削除" in label
    assert "投稿" not in label
    assert "復元できません" in body


@pytest.mark.integration
def test_quit_refused_while_billed_worker_runs(cfg: MeetingConfig, repo: Path) -> None:
    """課金・外部公開を伴うワーカーの実行中は終了を拒否すること。

    thread ワーカーは cancel_all では止まらない。ここで終了させると子プロセス（Unit B /
    mp4→VTT）が孤児化して**課金だけが進み**、結果も台帳も残らない。さらにインタプリタの終了が
    thread の join 待ちで最大 300 秒ブロックされ、操作者には「終了できないアプリ」に見える。
    """
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    release = asyncio.Event()

    def blocking_run(cmd, **kwargs):
        if (auth := _auth_response(cmd)) is not None:
            return auth
        # ワーカーを実行中のまま保持する（Slack/議事録の長時間処理を模す）。
        while not release.is_set():
            time.sleep(0.01)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    async def scenario() -> tuple[bool, str]:
        app = MeetingApp(cfg, run=blocking_run)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.press("m")  # 議事録ワーカー起動（blocking_run で滞留する）
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
            still_running = app.is_running
            text = "\n".join(app.query_one("#log", Log).lines)
            release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            return still_running, text

    still_running, text = _run_async(scenario())
    assert still_running, "課金ワーカー実行中は終了しないこと"
    assert "終了できません" in text


@pytest.mark.integration
def test_delete_session_refused_while_recording(cfg: MeetingConfig, repo: Path) -> None:
    """録音中は削除を受け付けないこと（進行中プロセスの足元でファイルを消さない）。"""
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)

    class _Running:
        def poll(self) -> int | None:
            return None

    async def scenario() -> str:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            app._recorder._process = _Running()  # type: ignore[assignment]
            await pilot.click("#delete-session")
            await pilot.pause()
            app._recorder._process = None  # on_unmount の停止処理へ入らせない
            return "\n".join(app.query_one("#log", Log).lines)

    text = _run_async(scenario())
    assert "削除できません" in text
    assert cfg.session_recording_dir(_SESSION).is_dir()


@pytest.mark.integration
def test_refresh_keeps_selection_when_it_is_not_row_zero(cfg: MeetingConfig, repo: Path) -> None:
    """再読込後も選択が保たれること（DataTable の RowHighlighted(0) で行 0 へ巻き戻らない）。

    `clear()` 直後の最初の `add_row` は RowHighlighted(0) を post_message で投げるため、
    ハンドラは action_refresh の復帰後に走り `_selected` を上書きする。取込由来のセッションIDは
    ファイル名 stem（英字）で数字の録音IDより後ろに並ぶ＝行 0 になり得ないので、取込直後の選択は
    必ず奪われる。その状態で共有すれば別会議の議事録を投稿し、議事録なら別録音へ課金する。
    """
    write_manifest(repo, "20260101-000000", self_sec=1, others_sec=1)
    write_manifest(repo, "20260303-000000", self_sec=1, others_sec=1)
    write_out_file(repo, "zz-imported", "final_transcript.json", "{}")  # 取込由来（末尾に並ぶ）

    async def scenario() -> tuple[str | None, int]:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = "zz-imported"
            app.action_refresh()
            await pilot.pause()  # メッセージポンプを回す（RowHighlighted はキュー経由）
            return app._selected, app.query_one("#table", DataTable).cursor_row

    selected, cursor_row = _run_async(scenario())
    assert selected == "zz-imported"
    assert cursor_row == 2, "表のカーソルも選択行に合っていること（見た目と操作対象の一致）"


@pytest.mark.integration
def test_delete_session_button_without_selection_shows_error(cfg: MeetingConfig) -> None:
    async def scenario() -> str:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#delete-session")
            await pilot.pause()
            return "\n".join(app.query_one("#log", Log).lines)

    text = _run_async(scenario())
    assert "選択されていません" in text


# --- ステータスバー / パイプライン段（#status・#pipeline） -------------------


@pytest.mark.integration
def test_status_bar_shows_selected_session_and_monthly_cost(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=120.0, others_sec=118.0)
    ledger.append(
        cfg.ledger_path,
        ledger.LedgerEntry(
            ts=ledger.now_iso(),
            session=_SESSION,
            stage="transcribe",
            backend="aws",
            est_usd=1.23,
            unit_price_usd=0.024,
            cumulative_month_usd=1.23,
        ),
    )

    async def scenario() -> tuple[str, str]:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            session_text = str(app.query_one("#status-session").render())
            cost_text = str(app.query_one("#status-cost").render())
            return session_text, cost_text

    session_text, cost_text = _run_async(scenario())
    assert session_text == f"#1 {_SESSION}"  # 1件しかないため通し番号は #1
    assert "$1.23" in cost_text
    assert "$50.00" in cost_text  # meeting.toml の月次閾値


@pytest.mark.integration
def test_pipeline_step_reflects_naming_required_stage(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=120.0, others_sec=118.0)
    write_out_file(repo, _SESSION, "speaker_names.json", "{}")

    async def scenario() -> tuple[bool, bool, bool, str]:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            record_step = app.query_one("#step-record")
            import_step = app.query_one("#step-import")
            minutes_step = app.query_one("#step-minutes")
            import_meta = str(import_step.query_one(".step-meta").render())
            return (
                record_step.has_class("-done"),
                import_step.has_class("-active"),
                minutes_step.has_class("-active") or minutes_step.has_class("-done"),
                import_meta,
            )

    record_done, import_active, minutes_touched, import_meta = _run_async(scenario())
    assert record_done
    assert import_active
    assert not minutes_touched  # まだ「議事録」段には到達していない
    assert "話者名の記入待ち" in import_meta


@pytest.mark.integration
def test_session_numbers_are_sequential_in_ascending_order(cfg: MeetingConfig, repo: Path) -> None:
    """セッション一覧はセッションID昇順。先頭列の通し番号はその順で1から振られる。

    先頭列は処理中マーカー（`●`／`◐`）を兼ねるため、待機中の行はマーカー幅の空白が前置される。
    """
    write_manifest(repo, "20260601-000000", self_sec=1, others_sec=1)
    write_manifest(repo, "20260615-000000", self_sec=1, others_sec=1)
    write_manifest(repo, "20260701-000000", self_sec=1, others_sec=1)

    async def scenario() -> list[tuple[str, str]]:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            table = app.query_one("#table", DataTable)
            return [
                (str(table.get_cell_at((row, 0))), str(table.get_cell_at((row, 1))))
                for row in range(table.row_count)
            ]

    rows = _run_async(scenario())
    assert rows == [
        ("  1", "20260601-000000"),
        ("  2", "20260615-000000"),
        ("  3", "20260701-000000"),
    ]
    assert all("●" not in number and "◐" not in number for number, _ in rows), "待機中はマーカーを出さない"


@pytest.mark.integration
def test_keybindings_trigger_same_actions_as_buttons(cfg: MeetingConfig, repo: Path) -> None:
    """i/m/s/x のキー入力が、対応するボタンと同じアクションを起動すること。

    キーごとに**異なる**観測結果で判定する。以前はセッション未選択で m と x を押し、どちらも
    「セッションが選択されていません」になる状態を 2 回 assert していたため、m のバインディングを
    削除してもテストが緑のままだった（x のメッセージが両方の assert を満たしていた）。
    """
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    write_out_file(repo, _SESSION, "minutes.md", "# 議事録")  # MINUTES_DONE にして m/s の応答を分ける

    async def scenario() -> tuple[str, bool, bool]:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.press("m")  # → action_minutes（生成済みなので専用メッセージ）
            await pilot.pause()
            await pilot.press("s")  # → action_slack（トークン未設定なのでモーダルは出ない）
            await pilot.pause()
            await pilot.press("x")  # → action_delete_session（削除確認モーダル）
            await pilot.pause()
            delete_modal_opened = app.screen.query("#confirm-body").first() is not None
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("i")  # → action_import_vtt（パス入力モーダル）
            await pilot.pause()
            path_input_opened = app.screen.query("#path-input").first() is not None
            await pilot.press("escape")
            await pilot.pause()
            return "\n".join(app.query_one("#log", Log).lines), delete_modal_opened, path_input_opened

    text, delete_modal_opened, path_input_opened = _run_async(scenario())
    assert "は議事録生成済みです" in text, "m → action_minutes"
    assert "SLACK_BOT_TOKEN が未設定" in text, "s → action_slack"
    assert delete_modal_opened, "x → action_delete_session"
    assert path_input_opened, "i → action_import_vtt"


@pytest.mark.integration
def test_toggle_log_action_hides_and_shows_table(cfg: MeetingConfig) -> None:
    async def scenario() -> tuple[bool, bool]:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            table = app.query_one("#table", DataTable)
            before = table.display
            await pilot.press("ctrl+l")
            await pilot.pause()
            hidden = table.display
            await pilot.press("ctrl+l")
            await pilot.pause()
            restored = table.display
            return before and not hidden, restored

    toggled_off, restored = _run_async(scenario())
    assert toggled_off
    assert restored


# --- キーバインド刷新（r=録音 / ^R=再読込 / 初期フォーカス / 上下ナビゲーション） -----


@pytest.mark.integration
def test_lowercase_r_key_toggles_recording(cfg: MeetingConfig) -> None:
    async def scenario() -> None:
        app = MeetingApp(cfg, run=_ok_runner())
        app._recorder._process = _FakeProcess(["x"])  # poll() が None のため録音中扱い
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("r")
            await pilot.pause()

    _run_async(scenario())
    assert cfg.stop_file.is_file()


@pytest.mark.integration
def test_ctrl_r_key_refreshes_session_list(cfg: MeetingConfig, repo: Path) -> None:
    async def scenario() -> str:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
            await pilot.press("ctrl+r")
            await pilot.pause()
            return "\n".join(app.query_one("#log", Log).lines)

    text = _run_async(scenario())
    assert "セッション一覧を更新しました（1 件）" in text


@pytest.mark.integration
def test_initial_focus_is_session_table(cfg: MeetingConfig) -> None:
    async def scenario() -> str | None:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            return app.focused.id if app.focused else None

    focused_id = _run_async(scenario())
    assert focused_id == "table"


@pytest.mark.integration
def test_up_down_navigate_between_table_and_record_minutes(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)

    async def scenario() -> tuple[str | None, str | None]:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app.query_one("#table", DataTable).focus()
            await pilot.pause()
            await pilot.press("up")
            await pilot.pause()
            after_up = app.focused.id if app.focused else None
            await pilot.press("down")
            await pilot.pause()
            after_down = app.focused.id if app.focused else None
            return after_up, after_down

    after_up, after_down = _run_async(scenario())
    assert after_up == "record-minutes"
    assert after_down == "table"


@pytest.mark.integration
def test_palette_commands_have_help_text(cfg: MeetingConfig) -> None:
    async def scenario() -> list[str | None]:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            provider = MeetingCommands(app.screen)
            return [hit.help async for hit in provider.discover()]

    helps = _run_async(scenario())
    assert len(helps) == 2
    assert all(helps)


@pytest.mark.integration
def test_logo_shows_repo_root_path(cfg: MeetingConfig) -> None:
    async def scenario() -> str:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            return str(app.query_one("#logo").render())

    text = _run_async(scenario())
    assert str(cfg.repo_root) in text


# --- 話者名ゲート（BR-NAME-01/02・FR-H2-03）と mp4 取込（FR-18） ------------------

_NAMING_TEMPLATE = {
    "sessionId": _SESSION,
    "mappings": {"spk_0": "", "self": "自分"},
    "unresolved": [],
    "_clusters": [{"label": "spk_0", "segmentCount": 42, "sampleUtterances": ["では始めましょう"]}],
}


def _staged_runner(steps, *, rcs=None):
    """呼び出し回数ごとに副作用を切り替えるフェイク runner（段が進む様子を再現する）。

    `steps` は (cmd, kwargs) を受ける callable のリスト。回数が超えたら最後の step を使い回す。
    """
    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        if (auth := _auth_response(cmd)) is not None:
            return auth
        index = len(calls)
        calls.append(list(cmd))
        steps[min(index, len(steps) - 1)](cmd, kwargs)
        rc = 0 if rcs is None else rcs[min(index, len(rcs) - 1)]
        return SimpleNamespace(returncode=rc, stdout="ok", stderr="")

    return _run, calls


def _write_naming_template(repo: Path, session: str = _SESSION) -> Path:
    return write_out_file(repo, session, "speaker_names.json", json.dumps(_NAMING_TEMPLATE, ensure_ascii=False))


def _write_transcript_and_minutes(repo: Path, session: str = _SESSION) -> None:
    write_out_file(repo, session, "final_transcript.json", json.dumps({"segments": [{"text": "x" * 1000}]}))
    write_out_file(repo, session, "minutes.md", "## 決定事項\n- 進める\n")


@pytest.mark.integration
def test_minutes_button_completes_wav_route_through_naming_modal(cfg: MeetingConfig, repo: Path) -> None:
    """本命の経路: 録音済セッションで「議事録作成」を1回押すと、命名ゲートで記入モーダルが
    自動的に開き、記入して続行するだけで minutes.md まで到達する（TUI を出ない）。
    """
    write_manifest(repo, _SESSION, self_sec=120.0, others_sec=120.0)
    run, calls = _staged_runner(
        [
            lambda cmd, kwargs: _write_naming_template(repo),  # 1回目: 命名ゲートで停止
            lambda cmd, kwargs: _write_transcript_and_minutes(repo),  # 2回目: 議事録完成
        ]
    )

    async def scenario() -> str:
        app = MeetingApp(cfg, run=run)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.click("#minutes-bedrock")
            await app.workers.wait_for_complete()
            await pilot.pause()
            # 命名ゲート到達で記入モーダルが自動的に開いていること。
            app.screen.query_one("#speaker-0", Input).value = "小松"
            await pilot.click("#ok")
            await pilot.pause()
            await pilot.click("#ok")  # 続く会議情報モーダルは空欄のまま続行
            await app.workers.wait_for_complete()
            await pilot.pause()
            return "\n".join(app.query_one("#log", Log).lines)

    text = _run_async(scenario())

    assert len(calls) == 2  # 記入を挟んでパイプラインが2回走る。
    assert all("paired" in cmd for cmd in calls)  # 録音由来なので paired 経路のまま。
    naming_json = json.loads((cfg.session_out_dir(_SESSION) / "speaker_names.json").read_text(encoding="utf-8"))
    assert naming_json["mappings"]["spk_0"] == "小松"
    assert naming_json["mappings"]["self"] == "自分"  # self は温存。
    assert "議事録が完成しました" in text


@pytest.mark.integration
def test_speaker_names_modal_does_not_leak_pii_to_log(cfg: MeetingConfig, repo: Path) -> None:
    """実名と代表発言はモーダル内のみ。ログには件数だけ出す（BR-NAME-04）。"""
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    _write_naming_template(repo)
    run, _ = _staged_runner([lambda cmd, kwargs: _write_transcript_and_minutes(repo)])

    async def scenario() -> str:
        app = MeetingApp(cfg, run=run)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.click("#minutes-bedrock")
            await pilot.pause()
            app.screen.query_one("#speaker-0", Input).value = "小松"
            await pilot.click("#ok")
            await pilot.pause()
            await pilot.click("#ok")  # 続く会議情報モーダルも空欄のまま続行
            await app.workers.wait_for_complete()
            await pilot.pause()
            return "\n".join(app.query_one("#log", Log).lines)

    text = _run_async(scenario())

    assert "話者名を 1 件記入しました" in text
    assert "小松" not in text  # 実名。
    assert "では始めましょう" not in text  # 代表発言。


@pytest.mark.integration
def test_speaker_names_modal_cancel_does_not_run_pipeline(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    _write_naming_template(repo)
    run, calls = _staged_runner([lambda cmd, kwargs: None])

    async def scenario() -> str:
        app = MeetingApp(cfg, run=run)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.click("#minutes-bedrock")
            await pilot.pause()
            await pilot.click("#cancel")
            await pilot.pause()
            return "\n".join(app.query_one("#log", Log).lines)

    text = _run_async(scenario())

    assert calls == []  # 課金段へ入らない。
    assert "話者名の記入を中止しました" in text


@pytest.mark.integration
def test_meeting_info_modal_saves_filled_fields_and_launches_pipeline(cfg: MeetingConfig, repo: Path) -> None:
    """話者名ゲート後の会議情報モーダルで記入すると meeting_info.json に保存され、続けて生成される。"""
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    _write_naming_template(repo)
    run, calls = _staged_runner([lambda cmd, kwargs: _write_transcript_and_minutes(repo)])

    async def scenario() -> str:
        app = MeetingApp(cfg, run=run)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.click("#minutes-bedrock")
            await pilot.pause()
            await pilot.click("#ok")  # 話者名は空欄のまま続行
            await pilot.pause()
            app.screen.query_one("#mi-title", Input).value = "定例会"
            app.screen.query_one("#mi-participants", Input).value = "田中、山田（A社）"
            await pilot.click("#ok")
            await app.workers.wait_for_complete()
            await pilot.pause()
            return "\n".join(app.query_one("#log", Log).lines)

    text = _run_async(scenario())

    assert len(calls) == 1
    info = json.loads((cfg.session_out_dir(_SESSION) / "meeting_info.json").read_text(encoding="utf-8"))
    assert info["title"] == "定例会"
    assert info["participants"] == ["田中", "山田（A社）"]
    assert "会議情報を保存しました" in text
    assert "田中" not in text  # 参加者名はログに出さない（BR-NAME-04 と同じ扱い）。


@pytest.mark.integration
def test_meeting_info_modal_skip_records_empty_file_and_continues(cfg: MeetingConfig, repo: Path) -> None:
    """会議情報モーダルをスキップしても続行し、「訊いた」ことを空ファイルで記録する。

    ファイルを残さないと「既存ファイルがあれば訊かない」判定に掛からず、以降の実行で毎回
    訊かれる（ウィザードは空入力でも保存する。CLI と TUI で挙動を分けない）。
    """
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    _write_naming_template(repo)
    run, calls = _staged_runner([lambda cmd, kwargs: _write_transcript_and_minutes(repo)])

    async def scenario() -> None:
        app = MeetingApp(cfg, run=run)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.click("#minutes-bedrock")
            await pilot.pause()
            await pilot.click("#ok")  # 話者名は空欄のまま続行
            await pilot.pause()
            await pilot.click("#cancel")  # 会議情報はスキップ
            await app.workers.wait_for_complete()
            await pilot.pause()

    _run_async(scenario())

    assert len(calls) == 1  # スキップしてもパイプラインは続行する。
    info_path = cfg.session_out_dir(_SESSION) / "meeting_info.json"
    assert info_path.is_file()
    assert json.loads(info_path.read_text(encoding="utf-8")) == {
        "title": "",
        "datetime": "",
        "participants": [],
    }


@pytest.mark.integration
def test_meeting_info_modal_not_reopened_when_file_already_exists(cfg: MeetingConfig, repo: Path) -> None:
    """meeting_info.json が既にあれば2回目以降は訊かない（毎回訊かれる煩わしさを避ける）。"""
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    _write_naming_template(repo)
    (cfg.session_out_dir(_SESSION)).mkdir(parents=True, exist_ok=True)
    (cfg.session_out_dir(_SESSION) / "meeting_info.json").write_text(
        json.dumps({"title": "既存の会議名", "datetime": "", "participants": []}), encoding="utf-8"
    )
    run, calls = _staged_runner([lambda cmd, kwargs: _write_transcript_and_minutes(repo)])

    async def scenario() -> bool:
        app = MeetingApp(cfg, run=run)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.click("#minutes-bedrock")
            await pilot.pause()
            await pilot.click("#ok")  # 話者名は空欄のまま続行
            await app.workers.wait_for_complete()
            await pilot.pause()
            return bool(app.screen.query("#mi-title"))

    modal_open = _run_async(scenario())

    assert modal_open is False
    assert len(calls) == 1


@pytest.mark.integration
def test_speaker_names_modal_blank_input_continues_with_labels(cfg: MeetingConfig, repo: Path) -> None:
    """空欄のまま続行すると spk_n のラベルで進む（記入は任意）。"""
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    _write_naming_template(repo)
    run, calls = _staged_runner([lambda cmd, kwargs: _write_transcript_and_minutes(repo)])

    async def scenario() -> str:
        app = MeetingApp(cfg, run=run)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.click("#minutes-bedrock")
            await pilot.pause()
            await pilot.click("#ok")  # 何も入力せず続行
            await pilot.pause()
            await pilot.click("#ok")  # 続く会議情報モーダルも空欄のまま続行
            await app.workers.wait_for_complete()
            await pilot.pause()
            return "\n".join(app.query_one("#log", Log).lines)

    text = _run_async(scenario())

    assert len(calls) == 1
    assert "spk_n のラベルのまま続行します" in text


@pytest.mark.integration
def test_auto_resume_stops_at_limit_when_pipeline_makes_no_progress(cfg: MeetingConfig, repo: Path) -> None:
    """段が進まない異常時に、モーダル→実行を無限に繰り返さないこと（_MAX_AUTO_RESUME）。"""
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    run, calls = _staged_runner([lambda cmd, kwargs: _write_naming_template(repo)])

    async def scenario() -> tuple[str, bool]:
        app = MeetingApp(cfg, run=run)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.click("#minutes-bedrock")
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.screen.query_one("#speaker-0", Input).value = "小松"
            await pilot.click("#ok")
            await pilot.pause()
            await pilot.click("#ok")  # 続く会議情報モーダルは空欄のまま続行
            await app.workers.wait_for_complete()
            await pilot.pause()
            modal_open = bool(app.screen.query("#speaker-0"))
            return "\n".join(app.query_one("#log", Log).lines), modal_open

    text, modal_open = _run_async(scenario())

    assert len(calls) == 2  # 自動再開は1回だけ。
    assert not modal_open  # 3回目のモーダルは開かない。
    assert "話者名の記入が必要です" in text  # 従来の案内へフォールバック。


@pytest.mark.integration
def test_import_vtt_resumes_through_naming_modal_with_vtt_mode(cfg: MeetingConfig, repo: Path) -> None:
    """取込由来でも命名ゲートから TUI 内で再開できること（再開は `--mode vtt`）。"""
    session = "meeting-2026-06-01"
    vtt_path = repo / f"{session}.vtt"
    vtt_path.write_text("WEBVTT\n", encoding="utf-8")
    run, calls = _staged_runner(
        [
            lambda cmd, kwargs: _write_naming_template(repo, session),
            lambda cmd, kwargs: _write_transcript_and_minutes(repo, session),
        ]
    )

    async def scenario() -> str:
        app = MeetingApp(cfg, run=run)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#import-vtt")
            await pilot.pause()
            app.screen.query_one("#path-input", Input).value = str(vtt_path)
            await pilot.click("#ok")
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.screen.query_one("#speaker-0", Input).value = "小松"
            await pilot.click("#ok")
            await pilot.pause()
            await pilot.click("#ok")  # 続く会議情報モーダルは空欄のまま続行
            await app.workers.wait_for_complete()
            await pilot.pause()
            return "\n".join(app.query_one("#log", Log).lines)

    text = _run_async(scenario())

    assert len(calls) == 2
    assert all("vtt" in cmd for cmd in calls)  # 再開も VTT 経路（paired へ落ちない）。
    assert (cfg.session_out_dir(session) / "minutes.md").is_file()
    assert "議事録が完成しました" in text


@pytest.mark.integration
def test_import_mp4_runs_mp4_pipeline_and_records_both_stages(cfg: MeetingConfig, repo: Path) -> None:
    """mp4 を取込に渡すと mp4 段（FR-18）→ VTT 経路（FR-17）へ接続され、台帳に2段が並ぶ。"""
    session = "recording-2026-06-01"
    mp4 = repo / f"{session}.mp4"
    mp4.write_bytes(b"\x00")

    def _run(cmd, **kwargs):
        if (auth := _auth_response(cmd)) is not None:
            return auth
        if cmd[0] == "ffprobe":
            return SimpleNamespace(returncode=0, stdout="600.0", stderr="")
        if any("mp4-to-vtt" in part for part in cmd):
            (repo / f"{session}.vtt").write_text("WEBVTT\n", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        _write_transcript_and_minutes(repo, session)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    async def scenario() -> str | None:
        app = MeetingApp(cfg, run=_run)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#import-vtt")
            await pilot.pause()
            app.screen.query_one("#path-input", Input).value = str(mp4)
            await pilot.click("#ok")
            await app.workers.wait_for_complete()
            await pilot.pause()
            return app._selected

    selected = _run_async(scenario())

    assert selected == session
    entries = ledger.load(cfg.ledger_path)
    assert {e.stage for e in entries} == {"transcribe", "bedrock"}


@pytest.mark.integration
def test_import_mp4_registers_generated_vtt_for_resume(cfg: MeetingConfig, repo: Path) -> None:
    """再開に使うのは生成した VTT（mp4 ではない）。mp4 を再実行すると Transcribe を二重課金する。"""
    session = "recording-2026-06-01"
    mp4 = repo / f"{session}.mp4"
    mp4.write_bytes(b"\x00")

    def _run(cmd, **kwargs):
        if (auth := _auth_response(cmd)) is not None:
            return auth
        if cmd[0] == "ffprobe":
            return SimpleNamespace(returncode=0, stdout="60.0", stderr="")
        if any("mp4-to-vtt" in part for part in cmd):
            (repo / f"{session}.vtt").write_text("WEBVTT\n", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        _write_naming_template(repo, session)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    async def scenario() -> Path | None:
        app = MeetingApp(cfg, run=_run)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#import-vtt")
            await pilot.pause()
            app.screen.query_one("#path-input", Input).value = str(mp4)
            await pilot.click("#ok")
            await app.workers.wait_for_complete()
            await pilot.pause()
            return app._minutes._vtt_sources.get(session)

    source = _run_async(scenario())
    assert source == repo / f"{session}.vtt"


@pytest.mark.integration
def test_import_mp4_aborts_when_ffprobe_missing(cfg: MeetingConfig, repo: Path) -> None:
    """ffprobe が無い＝見積が出せない場合、課金段へ入らず actionable なログで止まること。"""
    mp4 = repo / "recording.mp4"
    mp4.write_bytes(b"\x00")
    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        if (auth := _auth_response(cmd)) is not None:
            return auth
        calls.append(list(cmd))
        if cmd[0] == "ffprobe":
            raise FileNotFoundError("ffprobe")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    async def scenario() -> str:
        app = MeetingApp(cfg, run=_run)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#import-vtt")
            await pilot.pause()
            app.screen.query_one("#path-input", Input).value = str(mp4)
            await pilot.click("#ok")
            await app.workers.wait_for_complete()
            await pilot.pause()
            return "\n".join(app.query_one("#log", Log).lines)

    text = _run_async(scenario())

    assert [c[0] for c in calls] == ["ffprobe"]
    assert ledger.load(cfg.ledger_path) == []
    assert "ffprobe" in text


@pytest.mark.integration
def test_delete_session_clears_remembered_vtt_source(cfg: MeetingConfig, repo: Path) -> None:
    """同一ID（ファイル名 stem）で再取込したとき、削除済みセッションの入力パスが残らないこと。"""
    session = "meeting-2026-06-01"
    write_out_file(repo, session, "speaker_names.json", "{}")

    async def scenario() -> dict:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._minutes._vtt_sources[session] = repo / f"{session}.vtt"
            app._minutes._auto_resume_counts[session] = 1
            app._selected = session
            await pilot.click("#delete-session")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            return dict(app._minutes._vtt_sources)

    sources = _run_async(scenario())
    assert sources == {}


# --- モーダル本文のマークアップ無効化（承認したプレビューと投稿本文の一致） --------------

# Textual は `[...]` をスタイルタグとして解釈する。markup=True のままだと、ASCII のみで構成された
# 角括弧（`[TODO]` `[ap-northeast-1]` など。議事録・パスに実在する）は**黙って消える**。承認画面から
# 消えても投稿本文には残るため「見たものと送るものが違う」状態になり、承認の意味が崩れる。
# `[/]` はさらに悪く、レイアウト時に MarkupError を送出してアプリを落とす。
# （和文タグ `[検討]` や数字始まり `[2026-06-01]` は素通りするため、テストは ASCII タグで固定する。）
_BRACKET_MINUTES = "## 決定事項\n- [TODO] [ap-northeast-1] へ移設する\n- 期限は [/] 未定\n"
_BRACKET_SESSION = "meeting-[draft]"


@pytest.mark.integration
def test_table_passes_session_name_as_content_not_markup(cfg: MeetingConfig, repo: Path) -> None:
    """一覧のセッション名は Content で渡すこと（DataTable の markup 解釈で名前が消えるのを防ぐ）。

    DataTable は `str` セルをマークアップとして解釈し、`meeting-[draft]` の角括弧を黙って落とす。
    一覧から名前が消えると操作者が対象を選べない。`get_cell_at` は渡した値をそのまま返すため、
    値の文字列比較では `str` へ戻す変更を検出できない（＝型で固定する）。
    """
    write_out_file(repo, _BRACKET_SESSION, "speaker_names.json", "{}")

    async def scenario() -> object:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            return app.query_one("#table", DataTable).get_cell_at((0, 1))

    cell = _run_async(scenario())

    assert isinstance(cell, Content), "セッション名は Content で渡す（str だとマークアップ解釈される）"
    assert _BRACKET_SESSION in str(cell)


def _prepare_slack(cfg: MeetingConfig, repo: Path, minutes_md: str) -> MeetingConfig:
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    write_out_file(repo, _SESSION, "minutes.md", minutes_md)
    (cfg.repo_root / ".env").write_text("SLACK_BOT_TOKEN=xoxb-test\n", encoding="utf-8")
    return replace(cfg, slack_default_channel="C123")


@pytest.mark.integration
def test_slack_preview_keeps_bracketed_text_verbatim(cfg: MeetingConfig, repo: Path) -> None:
    """プレビューが議事録の角括弧をそのまま見せること（承認したものと送るものを一致させる）。"""
    cfg2 = _prepare_slack(cfg, repo, _BRACKET_MINUTES)

    async def scenario() -> str:
        app = MeetingApp(cfg2, run=_ok_runner(), post=lambda token, payload: {"ok": True, "ts": "1"})
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.click("#slack")
            await pilot.pause()
            return app.screen.query_one("#slack-preview", Static).visual.plain

    plain = _run_async(scenario())

    assert "[TODO]" in plain  # markup=True では黙って消える。
    assert "[ap-northeast-1]" in plain


@pytest.mark.integration
def test_slack_post_survives_auto_closing_tag_in_minutes(cfg: MeetingConfig, repo: Path) -> None:
    """`[/]` を含む議事録でもモーダルが崩れず投稿まで到達すること（MarkupError 回帰）。"""
    cfg2 = _prepare_slack(cfg, repo, _BRACKET_MINUTES)
    posted: list[dict] = []

    def fake_poster(token, payload):
        posted.append(dict(payload))
        return {"ok": True, "ts": "123.456"}

    async def scenario() -> str:
        app = MeetingApp(cfg2, run=_ok_runner(), post=fake_poster)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.click("#slack")
            await pilot.pause()
            plain = app.screen.query_one("#slack-preview", Static).visual.plain
            await pilot.click("#ok")
            await app.workers.wait_for_complete()
            await pilot.pause()
            return plain

    plain = _run_async(scenario())

    assert "[/]" in plain  # 自動クローズタグがそのまま表示されている（解釈されていない）。
    assert posted, "投稿まで到達すること"


@pytest.mark.integration
def test_delete_confirm_body_keeps_bracketed_session_name(cfg: MeetingConfig, repo: Path) -> None:
    """削除確認の本文は削除対象のパスを含む。角括弧付きのセッション名が消えないこと。

    消えると「何を削除するのか」を確認できないまま承認させることになる（取り消せない操作）。
    """
    write_out_file(repo, _BRACKET_SESSION, "speaker_names.json", "{}")

    async def scenario() -> str:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _BRACKET_SESSION
            await pilot.click("#delete-session")
            await pilot.pause()
            return app.screen.query_one("#confirm-body", Static).visual.plain

    plain = _run_async(scenario())

    assert _BRACKET_SESSION in plain


@pytest.mark.integration
def test_speaker_names_modal_title_keeps_bracketed_session_id(cfg: MeetingConfig, repo: Path) -> None:
    """命名モーダルのタイトル（セッションID = VTT/mp4 のファイル名由来）が消えないこと。"""
    vtt_path = repo / f"{_BRACKET_SESSION}.vtt"
    vtt_path.write_text("WEBVTT\n", encoding="utf-8")
    run, _ = _staged_runner([lambda cmd, kwargs: _write_naming_template(repo, _BRACKET_SESSION)])

    async def scenario() -> str:
        app = MeetingApp(cfg, run=run)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#import-vtt")
            await pilot.pause()
            app.screen.query_one("#path-input", Input).value = str(vtt_path)
            await pilot.click("#ok")
            await app.workers.wait_for_complete()
            await pilot.pause()
            return app.screen.query_one(".modal-title", Static).visual.plain

    plain = _run_async(scenario())

    assert _BRACKET_SESSION in plain


def _rendered_text(app: MeetingApp) -> str:
    """実際に画面へ描かれた文字列（SVG スクリーンショットのテキストノードを連結する）。

    ウィジェットが保持する値ではなく描画結果を見る必要がある。DataTable は str セルを
    マークアップとして解釈するため、`get_cell_at` は元の文字列を返すのに画面からは消える。
    """
    return "".join(re.findall(r">([^<>]*)<", app.export_screenshot()))


@pytest.mark.integration
def test_session_table_shows_bracketed_session_id(cfg: MeetingConfig, repo: Path) -> None:
    """一覧に角括弧付きセッション名が描かれること（消えると操作者が対象を選べない）。"""
    write_out_file(repo, _BRACKET_SESSION, "speaker_names.json", "{}")

    async def scenario() -> str:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            return _rendered_text(app)

    text = _run_async(scenario())

    assert "draft" in text  # markup=True の DataTable では黙って落ちる。


@pytest.mark.integration
def test_status_bar_shows_bracketed_session_id(cfg: MeetingConfig, repo: Path) -> None:
    """ステータスバーの選択セッション名が消えないこと。"""
    write_out_file(repo, _BRACKET_SESSION, "speaker_names.json", "{}")

    async def scenario() -> str:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _BRACKET_SESSION
            app._refresh_live_state()
            await pilot.pause()
            return app.query_one("#status-session", Static).visual.plain

    plain = _run_async(scenario())

    assert _BRACKET_SESSION in plain


@pytest.mark.integration
def test_logo_keeps_bracketed_repo_path(cfg: MeetingConfig) -> None:
    """ロゴのリポジトリパスが消えないこと（マイク色のマークアップは維持したまま）。"""
    cfg2 = replace(cfg, repo_root=Path("C:/work/[案件A]/AAA-[draft]-BBB"))

    async def scenario() -> str:
        app = MeetingApp(cfg2, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            return app.query_one("#logo", Static).visual.plain

    plain = _run_async(scenario())

    assert "AAA-[draft]-BBB" in plain  # 置換値が再パースされていないこと。
    assert "▟█▙" in plain  # マイク部分のマークアップは従来どおり効いている（タグは消えている）。
    assert MIC_RED not in plain


# --- 処理状態の可視化（一覧のマーカー / 録音開始時の選択移動） -------------------

_REC_SESSION = "20260804-170215"


class _HoldingProcess:
    """`sessionId=` を出した後、release されるまで stdout を閉じないフェイク Popen。

    録音中の状態を観測するために使う（`_FakeProcess` は行を出し切ると即終了扱いになる）。
    """

    def __init__(self, session_id: str, release: threading.Event) -> None:
        self._release = release
        self._returncode: int | None = None
        self.stdout = self._lines(session_id)

    def _lines(self, session_id: str):
        yield f"sessionId={session_id}"
        self._release.wait(5)  # テストがハングしないよう上限を置く

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        self._returncode = 0
        return 0

    def kill(self) -> None:
        self._returncode = -9


def _cell(app: MeetingApp, session_id: str, column: str) -> str:
    """列キーでセルを読む（列の並び順に依存しない）。"""
    return str(app.query_one("#table", DataTable).get_cell(session_id, column))


def _freeze_blink(app: MeetingApp) -> None:
    """点滅の自動位相送りを止める（記号を決め打ちで検証するテスト用）。

    止めないと 0.6 秒周期のタイマーが `pilot.pause()` 中に割り込み、`●` を期待した箇所で
    `○` を観測する（負荷次第で落ちる不安定なテストになる）。
    """
    assert app._blink_timer is not None
    app._blink_timer.pause()


async def _wait_until(pilot: Pilot[None], predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    """条件が満たされるまでメッセージポンプを回す（満たされないまま timeout なら AssertionError）。

    録音の `sessionId=` はワーカースレッドが読んでから `call_from_thread` で渡るため、
    `pilot.pause()` 1 回では間に合わないことがある（負荷が高いと落ちる不安定なテストになる）。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError("条件が満たされないままタイムアウトしました。")


@pytest.mark.integration
def test_recording_start_lists_new_session_and_selects_it(cfg: MeetingConfig, repo: Path) -> None:
    """録音を始めたら一覧に新セッションが現れ、選択がそこへ移ること。

    移らないと前セッションが選ばれたまま残り、停止直後に議事録生成を押すと別の録音へ課金する。
    録音中セッションは manifest 未生成のため `runner.list_sessions` には出ない（表示側で合成する）。
    """
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)  # 先に別セッションが選ばれている状態
    _make_dist_exe(cfg)
    release = threading.Event()

    async def scenario() -> tuple[str | None, list[str], int, str, str | None]:
        app = MeetingApp(cfg, run=_ok_runner(), popen=lambda *a, **k: _HoldingProcess(_REC_SESSION, release))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            _freeze_blink(app)  # ● を決め打ちで見るため位相を固定する
            before = app._selected  # 前提: 既存セッションが既定選択
            await pilot.click("#record-toggle")
            await _wait_until(pilot, lambda: app._recorder.session_id == _REC_SESSION)
            table = app.query_one("#table", DataTable)
            ids = [str(table.get_cell_at((row, 1))) for row in range(table.row_count)]
            result = (
                app._selected,
                ids,
                table.cursor_row,
                _cell(app, _REC_SESSION, "number"),
                before,
            )
            release.set()
            await app.workers.wait_for_complete()
            return result

    selected, ids, cursor_row, number_text, before = _run_async(scenario())

    assert before == _SESSION
    assert selected == _REC_SESSION
    assert _REC_SESSION in ids
    assert cursor_row == ids.index(_REC_SESSION), "表のカーソルも新セッションに合っていること"
    assert "●" in number_text


@pytest.mark.integration
def test_recording_session_row_shows_recording_stage(cfg: MeetingConfig) -> None:
    """録音中の行は段の列に「録音中」を出す（manifest 未生成の「未録音」ではない）。"""
    _make_dist_exe(cfg)
    release = threading.Event()

    async def scenario() -> tuple[str, str]:
        app = MeetingApp(cfg, run=_ok_runner(), popen=lambda *a, **k: _HoldingProcess(_REC_SESSION, release))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#record-toggle")
            await _wait_until(pilot, lambda: app._recorder.session_id == _REC_SESSION)
            result = (_cell(app, _REC_SESSION, "stage"), _cell(app, _REC_SESSION, "status"))
            release.set()
            await app.workers.wait_for_complete()
            return result

    stage_text, status_text = _run_async(scenario())

    assert stage_text == "録音中（進行中）"
    assert status_text == "録音中"


@pytest.mark.integration
def test_recording_marker_blinks(cfg: MeetingConfig) -> None:
    """録音中マーカーが点滅すること（タイマーでセルを差し替える）。

    ANSI の blink 属性は端末側の設定で無視されるため、点滅は自前のタイマーで作る。
    ここでは位相の進み方（点灯 → 消灯 → 点灯）を決定的に確かめる。

    実タイマーは止めてから手で位相を進める。止めないと `pilot.pause()` の最中に 0.6 秒周期の
    タイマーが割り込んで位相が余分に反転し、観測結果が負荷次第で変わる（不安定なテストになる）。
    """
    _make_dist_exe(cfg)
    release = threading.Event()

    async def scenario() -> list[str]:
        app = MeetingApp(cfg, run=_ok_runner(), popen=lambda *a, **k: _HoldingProcess(_REC_SESSION, release))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            _freeze_blink(app)  # 自動の位相送りを止める（下で手動に切り替える）
            await pilot.click("#record-toggle")
            await _wait_until(pilot, lambda: app._recorder.session_id == _REC_SESSION)
            phases = [_cell(app, _REC_SESSION, "number")]
            for _ in range(2):
                app._toggle_blink()
                await pilot.pause()
                phases.append(_cell(app, _REC_SESSION, "number"))
            release.set()
            await app.workers.wait_for_complete()
            return phases

    phases = _run_async(scenario())

    assert phases == ["● 1", "○ 1", "● 1"]


def _marker_colours(app: MeetingApp) -> list[str | None]:
    """画面に描かれたマーカー（●/◐/○）の色を拾う。

    セルの値ではなく**描画結果**を見る。DataTable は既定（`cursor_foreground_priority="css"`）だと
    `.datatable--cursor` の color でセルの色を上書きするため、値としては色を持っていても
    選択行では画面に出ない。
    """
    colours: list[str | None] = []
    for strip in app.screen._compositor.render_strips():
        for segment in strip:
            if segment.text in ("●", "◐", "○"):
                style = segment.style
                colours.append(style.color.name if style is not None and style.color is not None else None)
    return colours


@pytest.mark.integration
def test_recording_marker_keeps_record_colour_on_selected_row(cfg: MeetingConfig) -> None:
    """選択行でもマーカーが録音色で描かれること。

    録音開始時は当の録音セッションを選択状態にするため、カーソル行で色が消えると
    「赤い印で録音中が分かる」という狙いがいちばん効くべき行で効かなくなる。
    """
    _make_dist_exe(cfg)
    release = threading.Event()

    async def scenario() -> tuple[list[str | None], int]:
        app = MeetingApp(cfg, run=_ok_runner(), popen=lambda *a, **k: _HoldingProcess(_REC_SESSION, release))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#record-toggle")
            await _wait_until(pilot, lambda: app._recorder.session_id == _REC_SESSION)
            table = app.query_one("#table", DataTable)
            result = (_marker_colours(app), table.cursor_row)
            release.set()
            await app.workers.wait_for_complete()
            return result

    colours, cursor_row = _run_async(scenario())

    assert cursor_row == 0, "録音セッションが選択（カーソル）行であること"
    assert MIC_RED in colours


@pytest.mark.integration
def test_blink_timer_is_registered_and_idle_list_does_not_blink(cfg: MeetingConfig, repo: Path) -> None:
    """点滅タイマーは常設だが、動いているセッションが無い間は再描画しないこと。"""
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)

    async def scenario() -> tuple[bool, str, bool]:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            has_timer = app._blink_timer is not None
            before = _cell(app, _SESSION, "number")
            app._toggle_blink()
            await pilot.pause()
            return has_timer, before, app._blink_lit

    has_timer, before, still_lit = _run_async(scenario())

    assert has_timer, "点滅タイマーが張られていること"
    assert before == "  1"
    assert still_lit, "処理中が無いときは位相を進めない（無用な再描画を避ける）"


@pytest.mark.integration
def test_minutes_refused_while_selected_session_is_recording(cfg: MeetingConfig) -> None:
    """録音中セッションで議事録生成を押しても、取込由来と誤判定した案内を出さないこと。"""
    _make_dist_exe(cfg)
    release = threading.Event()

    async def scenario() -> str:
        app = MeetingApp(cfg, run=_ok_runner(), popen=lambda *a, **k: _HoldingProcess(_REC_SESSION, release))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#record-toggle")
            await _wait_until(pilot, lambda: app._recorder.session_id == _REC_SESSION)
            await pilot.press("m")
            await pilot.pause()
            text = "\n".join(app.query_one("#log", Log).lines)
            release.set()
            await app.workers.wait_for_complete()
            return text

    text = _run_async(scenario())

    assert "録音中です。停止してから議事録を作成してください。" in text
    assert "入力ファイルを保持していません" not in text  # 取込由来と誤判定した案内を出さない


@pytest.mark.integration
def test_running_minutes_marks_row_and_clears_on_completion(cfg: MeetingConfig, repo: Path) -> None:
    """議事録生成中の行にマーカーと実行中の段を出し、完了したら消すこと。"""
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    release = asyncio.Event()

    def blocking_run(cmd, **kwargs):
        if (auth := _auth_response(cmd)) is not None:
            return auth
        while not release.is_set():
            time.sleep(0.01)
        _write_transcript_and_minutes(repo)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    async def scenario() -> tuple[str, str, str]:
        app = MeetingApp(cfg, run=blocking_run)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            _freeze_blink(app)  # ◐ を決め打ちで見るため位相を固定する
            app._selected = _SESSION
            await pilot.press("m")
            await pilot.pause()
            running_number = _cell(app, _SESSION, "number")
            running_stage = _cell(app, _SESSION, "stage")
            release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            return running_number, running_stage, _cell(app, _SESSION, "number")

    running_number, running_stage, done_number = _run_async(scenario())

    assert "◐" in running_number
    assert running_stage == "議事録生成中…"
    assert "◐" not in done_number, "完了後はマーカーを消すこと"


@pytest.mark.integration
def test_minutes_button_shows_running_caption(cfg: MeetingConfig, repo: Path) -> None:
    """議事録作成中は「議事録作成」ボタンのキャプションを「議事録作成中」にすること。"""
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    release = asyncio.Event()

    def blocking_run(cmd, **kwargs):
        if (auth := _auth_response(cmd)) is not None:
            return auth
        while not release.is_set():
            time.sleep(0.01)
        _write_transcript_and_minutes(repo)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    async def scenario() -> tuple[str, bool, str, str]:
        app = MeetingApp(cfg, run=blocking_run)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.press("m")
            await pilot.pause()
            minutes_button = app.query_one("#minutes-bedrock", Button)
            running = (str(minutes_button.label), minutes_button.has_class("-running"))
            # 議事録生成中に取込ボタンまで「取込中」にしない。
            import_label = str(app.query_one("#import-vtt", Button).label)
            release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            return (*running, import_label, str(minutes_button.label))

    label, marked, import_label, after = _run_async(scenario())

    assert label == "議事録作成中 (m)"
    assert marked, "実行中を示すクラスを付けること"
    assert import_label == "取込 VTT/mp4 (i)"
    assert after == "議事録作成 (m)", "完了後は元のキャプションへ戻すこと"


@pytest.mark.integration
def test_import_button_shows_running_caption(cfg: MeetingConfig, repo: Path) -> None:
    """取込中は「取込 VTT/mp4」のキャプションを「取込中」にすること。"""
    vtt = repo / "meeting-2026-08-04.vtt"
    vtt.write_text("WEBVTT\n", encoding="utf-8")
    release = asyncio.Event()

    def blocking_run(cmd, **kwargs):
        if (auth := _auth_response(cmd)) is not None:
            return auth
        while not release.is_set():
            time.sleep(0.01)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    async def scenario() -> tuple[str, str, str]:
        app = MeetingApp(cfg, run=blocking_run)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#import-vtt")
            await pilot.pause()
            app.screen.query_one("#path-input", Input).value = str(vtt)
            await pilot.click("#ok")
            await pilot.pause()
            import_button = app.query_one("#import-vtt", Button)
            running = str(import_button.label)
            minutes_label = str(app.query_one("#minutes-bedrock", Button).label)
            release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            return running, minutes_label, str(import_button.label)

    running, minutes_label, after = _run_async(scenario())

    assert running == "取込中 (i)"
    assert minutes_label == "議事録作成 (m)"
    assert after == "取込 VTT/mp4 (i)"


@pytest.mark.integration
def test_minutes_on_imported_session_does_not_say_importing(cfg: MeetingConfig, repo: Path) -> None:
    """取込由来セッションの議事録生成を「取込中」と表示しないこと。

    この経路は `--mode vtt`（取込と同じワーカーグループ）で走るため、グループを根拠に
    表示を決めると議事録生成中に「取込中」と出る。処理の種別で判定する実装の回帰防止。
    """
    session = "meeting-2026-08-04"
    write_out_file(repo, session, "final_transcript.json", json.dumps({"segments": [{"text": "x" * 100}]}))
    release = asyncio.Event()

    def blocking_run(cmd, **kwargs):
        if (auth := _auth_response(cmd)) is not None:
            return auth
        while not release.is_set():
            time.sleep(0.01)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    async def scenario() -> tuple[str, str, str]:
        app = MeetingApp(cfg, run=blocking_run)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._minutes._vtt_sources[session] = repo / f"{session}.vtt"  # 取込済みの記憶を模す
            app._selected = session
            await pilot.press("m")
            await pilot.pause()
            result = (
                str(app.query_one("#minutes-bedrock", Button).label),
                str(app.query_one("#import-vtt", Button).label),
                _cell(app, session, "stage"),
            )
            release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            return result

    minutes_label, import_label, stage_text = _run_async(scenario())

    assert minutes_label == "議事録作成中 (m)"
    assert import_label == "取込 VTT/mp4 (i)"
    assert stage_text == "議事録生成中…"


@pytest.mark.integration
def test_activity_marker_is_cleared_when_worker_raises(cfg: MeetingConfig, repo: Path) -> None:
    """ワーカーが例外終了してもマーカーを残さないこと。

    処理中を「セッションID → 状態」で持つと、例外時は戻り値 `(session_id, rc)` が無いため
    解除できず、一覧が処理中を出し続ける。グループ単位で持つ実装の回帰防止。
    """
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)

    def exploding_run(cmd, **kwargs):
        if (auth := _auth_response(cmd)) is not None:
            return auth
        raise RuntimeError("subprocess boom")

    async def scenario() -> tuple[str, str, dict[str, str]]:
        app = MeetingApp(cfg, run=exploding_run)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.press("m")
            await app.workers.wait_for_complete()
            await pilot.pause()
            return (
                _cell(app, _SESSION, "number"),
                "\n".join(app.query_one("#log", Log).lines),
                dict(app._activities),
            )

    number_text, log_text, activities = _run_async(scenario())

    assert "◐" not in number_text
    assert activities == {}
    assert "例外終了" in log_text


@pytest.mark.integration
def test_completed_steps_keep_their_own_meta(cfg: MeetingConfig, repo: Path) -> None:
    """段の注記は段ごとに自分の状態を述べること（04 共有に「議事録生成済」を出さない）。"""
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    write_pipeline_outputs(repo, _SESSION)  # minutes.md まで到達＝MINUTES_DONE

    async def scenario() -> list[str]:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            return [
                str(app.query_one(f"#step-{key}").query_one(".step-meta").render())
                for key in ("record", "import", "minutes", "share")
            ]

    metas = _run_async(scenario())

    assert metas == ["録音済", "文字起こし済", "議事録生成済", "未共有"]


# --- AWS 資格情報の確認（FR-H2-11） ---------------------------------------------


def _auth_runner(stdout: str, *, returncode: int = 0):
    """`--check-auth` に定型応答を返すフェイク runner（呼ばれたコマンドも記録する）。"""
    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        calls.append(list(cmd))
        if "--check-auth" in cmd:
            return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    return _run, calls


@pytest.mark.integration
def test_startup_shows_aws_profile_and_region(cfg: MeetingConfig) -> None:
    """起動時に profile と region をステータスバーへ出す（会議前に向き先を確かめられるように）。"""
    run, calls = _auth_runner("authProfile=subtext-dev\nauthRegion=ap-northeast-1\nauthStatus=ok\n")

    async def scenario() -> str:
        app = MeetingApp(cfg, run=run)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            return str(app.query_one("#status-aws").render())

    text = _run_async(scenario())

    assert text == "AWS subtext-dev/ap-northeast-1 ✓"
    assert ["uv", "run", "subtext-postmeeting", "--check-auth"] in calls


@pytest.mark.integration
def test_expired_credentials_are_shown_with_remediation(cfg: MeetingConfig) -> None:
    """失効時は画面に印を出し、Unit B の対処メッセージをログへ流すこと。"""
    run, _calls = _auth_runner(
        "authProfile=default\nauthRegion=ap-northeast-1\nauthStatus=expired\n"
        "AWS 認証が無効または期限切れです。`! aws login`（または `aws sso login`）で再認証してください。\n",
        returncode=1,
    )

    async def scenario() -> tuple[str, bool, str]:
        app = MeetingApp(cfg, run=run)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            widget = app.query_one("#status-aws", Static)
            return str(widget.render()), widget.has_class("-warn"), "\n".join(app.query_one("#log", Log).lines)

    text, warned, log_text = _run_async(scenario())

    assert "✗" in text and "認証切れ" in text
    assert warned, "注意色（$mt-warn）を当てること"
    assert "aws login" in log_text
    assert "⚠ AWS 認証" in log_text


@pytest.mark.integration
def test_auth_check_does_not_block_startup(cfg: MeetingConfig) -> None:
    """確認が返る前でも画面は操作できる（確認中の表示にとどめる）。"""
    release = asyncio.Event()

    def slow_run(cmd, **kwargs):
        if "--check-auth" in cmd:
            while not release.is_set():
                time.sleep(0.01)
            return SimpleNamespace(returncode=0, stdout="authStatus=ok\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    async def scenario() -> tuple[str, bool]:
        app = MeetingApp(cfg, run=slow_run)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            checking = str(app.query_one("#status-aws").render())
            clicked = await pilot.click("#refresh")  # 確認中でも操作できる
            release.set()
            await app.workers.wait_for_complete()
            return checking, clicked

    checking, clicked = _run_async(scenario())

    assert checking == "AWS 確認中…"
    assert clicked


@pytest.mark.integration
def test_a_key_rechecks_credentials(cfg: MeetingConfig) -> None:
    """再認証後に `a` で再確認できること（再認証自体はハーネスから起動しない）。"""
    outputs = [
        "authProfile=default\nauthRegion=ap-northeast-1\nauthStatus=expired\n",
        "authProfile=default\nauthRegion=ap-northeast-1\nauthStatus=ok\n",
    ]
    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        if "--check-auth" not in cmd:
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        stdout = outputs[min(len(calls), len(outputs) - 1)]
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    async def scenario() -> tuple[str, str]:
        app = MeetingApp(cfg, run=_run)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            before = str(app.query_one("#status-aws").render())
            await pilot.press("a")
            await app.workers.wait_for_complete()
            await pilot.pause()
            return before, str(app.query_one("#status-aws").render())

    before, after = _run_async(scenario())

    assert "✗" in before
    assert after == "AWS default/ap-northeast-1 ✓"
    assert len(calls) == 2


@pytest.mark.integration
def test_edit_minutes_button_opens_editor_and_logs_missing_headings(
    cfg: MeetingConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """議事録編集ボタンはエディタ相当（seam）を起動し、保存後に見出し欠落を警告する（②）。"""
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    write_pipeline_outputs(repo, _SESSION)

    def fake_launch(path: Path) -> None:
        path.write_text("本文だけになった\n", encoding="utf-8")

    monkeypatch.setattr("meeting.tui.edit_mod.default_launcher", lambda editor: fake_launch)

    async def scenario() -> str:
        app = MeetingApp(cfg, run=lambda *a, **k: None)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.click("#edit-minutes")
            await app.workers.wait_for_complete()
            await pilot.pause()
            return "\n".join(app.query_one("#log", Log).lines)

    text = _run_async(scenario())

    assert "議事録を保存しました" in text
    assert "決定事項" in text and "ToDo" in text
    minutes_path = cfg.session_out_dir(_SESSION) / "minutes.md"
    assert minutes_path.read_text(encoding="utf-8") == "本文だけになった\n"


@pytest.mark.integration
def test_edit_minutes_rejected_when_no_minutes_yet(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    _write_naming_template(repo)  # minutes.md は無い

    async def scenario() -> str:
        app = MeetingApp(cfg, run=lambda *a, **k: None)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.click("#edit-minutes")
            await pilot.pause()
            return "\n".join(app.query_one("#log", Log).lines)

    text = _run_async(scenario())

    assert "まだ議事録がありません" in text


@pytest.mark.integration
def test_import_materials_copies_resolved_files_into_materials_dir(cfg: MeetingConfig, repo: Path, tmp_path: Path) -> None:
    """資料投入モーダルで入力したパスが materials/ へコピーされること（③付帯資料）。"""
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    src = tmp_path / "agenda.txt"
    src.write_text("会議の前提資料", encoding="utf-8")

    async def scenario() -> str:
        app = MeetingApp(cfg, run=lambda *a, **k: None)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.click("#import-materials")
            await pilot.pause()
            app.screen.query_one("#materials-input", TextArea).text = str(src)
            await pilot.click("#ok")
            await pilot.pause()
            return "\n".join(app.query_one("#log", Log).lines)

    text = _run_async(scenario())

    assert "1 件を投入しました" in text
    materials_dir = cfg.session_out_dir(_SESSION) / "materials"
    assert (materials_dir / "agenda.txt").read_text(encoding="utf-8") == "会議の前提資料"


@pytest.mark.integration
def test_import_materials_cancel_does_not_copy(cfg: MeetingConfig, repo: Path, tmp_path: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    src = tmp_path / "agenda.txt"
    src.write_text("本文", encoding="utf-8")

    async def scenario() -> str:
        app = MeetingApp(cfg, run=lambda *a, **k: None)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.click("#import-materials")
            await pilot.pause()
            app.screen.query_one("#materials-input", TextArea).text = str(src)
            await pilot.click("#cancel")
            await pilot.pause()
            return "\n".join(app.query_one("#log", Log).lines)

    text = _run_async(scenario())

    assert "中止しました" in text
    assert not (cfg.session_out_dir(_SESSION) / "materials").is_dir()


@pytest.mark.integration
def test_slack_preview_matches_actual_thread_body(cfg: MeetingConfig, repo: Path) -> None:
    """プレビューのスレッド全文が実投稿と同じ内容であること（M-3）。

    `minutes_md` をそのまま見せると、実際の投稿（決定事項・ToDo を除き注記を付けた本文）と
    食い違い、承認したものと送るものが違う状態になる（CLI 側は既に是正済み）。
    """
    minutes = (
        "# 定例会\n- 日時: 2026-08-26 10:00\n\n"
        "## 決定事項\n- 価格を決定（12:30）\n\n"
        "## ToDo\n- [ ] 価格表を更新\n\n"
        "## 論点・議論サマリ\n### 価格帯\n- 議論の中身\n"
    )
    cfg2 = _prepare_slack(cfg, repo, minutes)

    async def scenario() -> str:
        app = MeetingApp(cfg2, run=_ok_runner(), post=lambda token, payload: {"ok": True, "ts": "1"})
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.click("#slack")
            await pilot.pause()
            return app.screen.query_one("#slack-preview", Static).visual.plain

    plain = _run_async(scenario())
    expected_body = slack.to_mrkdwn(slack.build_thread_body(minutes))

    assert expected_body in plain
    # 親で表示済みの決定事項見出しはスレッド側から除かれ、その旨の注記が入る。
    assert "決定事項・ToDo は上の要約メッセージをご覧ください" in plain


@pytest.mark.integration
def test_quit_is_refused_while_editing_minutes(cfg: MeetingConfig, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """編集中（外部エディタ起動中）は終了を拒否する（M-5）。

    thread ワーカーは cancel_all では止まらないため、終了させるとエディタを閉じるまで
    join 待ちで TUI が無反応になる。
    """
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    write_pipeline_outputs(repo, _SESSION)
    release = threading.Event()

    def blocking_launch(path: Path) -> None:
        release.wait(timeout=5)

    monkeypatch.setattr("meeting.tui.edit_mod.default_launcher", lambda editor: blocking_launch)

    async def scenario() -> tuple[str, bool]:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.click("#edit-minutes")
            await pilot.pause()
            await app.action_quit()  # 編集中なので拒否される
            await pilot.pause()
            text = "\n".join(app.query_one("#log", Log).lines)
            running = app.is_running
            release.set()
            await app.workers.wait_for_complete()
            return text, running

    text, still_running = _run_async(scenario())

    assert "議事録を編集中です" in text
    assert still_running is True  # 終了していない


@pytest.mark.integration
def test_delete_is_refused_while_editing_minutes(cfg: MeetingConfig, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """編集中のセッションは削除できない（M-5: 編集中のファイルを足元から消さない）。"""
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    write_pipeline_outputs(repo, _SESSION)
    release = threading.Event()

    def blocking_launch(path: Path) -> None:
        release.wait(timeout=5)

    monkeypatch.setattr("meeting.tui.edit_mod.default_launcher", lambda editor: blocking_launch)

    async def scenario() -> str:
        app = MeetingApp(cfg, run=_ok_runner())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected = _SESSION
            await pilot.click("#edit-minutes")
            await pilot.pause()
            await pilot.click("#delete-session")
            await pilot.pause()
            text = "\n".join(app.query_one("#log", Log).lines)
            release.set()
            await app.workers.wait_for_complete()
            return text

    text = _run_async(scenario())

    assert "削除できません" in text
    assert (cfg.session_out_dir(_SESSION) / "minutes.md").is_file()
