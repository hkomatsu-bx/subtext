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
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from textual.content import Content
from textual.widgets import Button, DataTable, Input, Log, Static

from meeting import ledger
from meeting.config import MeetingConfig
from meeting.tui import MeetingApp
from meeting.tui_view import MIC_RED, MeetingCommands, strip_path_quotes

# conftest を直接参照する（詳細は test_runner.py の同 import の注記を参照）。
from conftest import write_manifest, write_out_file, write_pipeline_outputs

_SESSION = "20260625-120156"


def _ok_runner(side_effect=None):
    def _run(cmd, **kwargs):
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
    """セッション一覧はセッションID昇順。先頭列の通し番号はその順で1から振られる。"""
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
        ("1", "20260601-000000"),
        ("2", "20260615-000000"),
        ("3", "20260701-000000"),
    ]


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
