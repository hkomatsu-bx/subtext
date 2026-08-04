"""録音プロセス制御（tui_recording）の単体テスト（Textual を起動しない）。

セッション ID は recorder の stdout（`sessionId=` 行）から取る。これは Unit A 側の契約
（03-unit-a-録音.md）で、一覧の行と選択対象を決める根拠になるため書式まで照合する。
"""

from __future__ import annotations

import pytest

from meeting.config import MeetingConfig
from meeting.tui_recording import RecordingController, parse_session_id


@pytest.mark.unit
def test_parse_session_id_reads_recorder_contract_line() -> None:
    assert parse_session_id("sessionId=20260804-170215") == "20260804-170215"


@pytest.mark.unit
def test_parse_session_id_ignores_other_lines() -> None:
    assert parse_session_id("stopFile=C:\\data\\.stop") is None
    assert parse_session_id("Recording session 20260804-170215. Press Ctrl+C") is None
    assert parse_session_id("[self/mic]    マイク (JBL)") is None


@pytest.mark.unit
def test_parse_session_id_rejects_malformed_id() -> None:
    """書式（yyyyMMdd-HHmmss）に合わない値は採用しない。

    ID はそのまま一覧の行キー・選択対象・削除確認の表示対象になる。想定外の文字列を通すと、
    実在しないセッションを選択したまま操作させることになる。
    """
    assert parse_session_id("sessionId=X") is None
    assert parse_session_id("sessionId=") is None
    assert parse_session_id("sessionId=2026-08-04") is None


class _FakeProcess:
    def __init__(self, lines: list[str]) -> None:
        self.stdout = iter(lines)
        self._returncode: int | None = None

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        self._returncode = 0
        return 0


def _controller(cfg: MeetingConfig, lines: list[str]) -> RecordingController:
    cfg.recorder_dist_exe.parent.mkdir(parents=True, exist_ok=True)
    cfg.recorder_dist_exe.write_text("exe", encoding="utf-8")  # ビルドを走らせない
    return RecordingController(cfg, run=lambda *a, **k: None, popen=lambda *a, **k: _FakeProcess(lines))


@pytest.mark.unit
def test_session_id_is_reported_once_while_recording(cfg: MeetingConfig) -> None:
    seen: list[str] = []
    controller = _controller(cfg, ["[self/mic] マイク", "sessionId=20260804-170215", "Recording session ..."])

    controller.run_until_exit(1, emit=lambda _line: None, on_started=lambda: None, on_session_id=seen.append)

    assert seen == ["20260804-170215"]


@pytest.mark.unit
def test_previous_session_id_is_dropped_before_process_becomes_live(cfg: MeetingConfig) -> None:
    """新しいプロセスが生きた瞬間に、前回の ID を返さないこと。

    前回の実行が stdout 走査中に落ちると `_session_id` が残る。代入順を誤ると
    「プロセスは新しい・ID は前回のもの」の一瞬が生まれ、別セッションの行が録音中になる。
    """
    controller = _controller(cfg, ["sessionId=20260804-170215"])
    controller._session_id = "20260101-000000"  # 中断した前回の残骸
    seen: list[str | None] = []

    controller.run_until_exit(
        1,
        emit=lambda _line: None,
        on_started=lambda: seen.append(controller.session_id),  # プロセスは既に生きている
    )

    assert seen == [None]


@pytest.mark.unit
def test_session_id_is_dropped_after_process_exits(cfg: MeetingConfig) -> None:
    """終了後も持ち続けると、一覧が「録音中」を出し続ける。"""
    controller = _controller(cfg, ["sessionId=20260804-170215"])

    controller.run_until_exit(1, emit=lambda _line: None, on_started=lambda: None)

    assert controller.session_id is None
    assert not controller.is_recording()
