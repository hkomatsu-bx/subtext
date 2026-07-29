"""B1F: ハーネス live 段のテスト（純粋な解決/構築＋_cmd_live の統合。実 AWS/実機不要）。"""

from __future__ import annotations

import subprocess
from datetime import datetime

from meeting import cli, runner
from meeting.config import LIVE_EXE_NAME, MeetingConfig


def test_new_session_id_formats_timestamp() -> None:
    sid = runner.new_session_id(datetime(2026, 7, 12, 4, 32, 15))
    assert sid == "20260712-043215"


def test_config_live_paths(cfg: MeetingConfig) -> None:
    sid = "20260712-043215"
    assert cfg.live_captions_path(sid) == cfg.out_dir / sid / "live_captions.jsonl"
    assert cfg.live_stop_file(sid) == cfg.out_dir / sid / ".stop"


def test_build_live_env_injects_sink_and_stop_paths(cfg: MeetingConfig) -> None:
    env = runner.build_live_env(cfg, "sess1")
    assert env["SUBTEXT_Live__CaptionSinkPath"] == str(cfg.live_captions_path("sess1"))
    assert env["SUBTEXT_Live__StopFilePath"] == str(cfg.live_stop_file("sess1"))


def test_resolve_live_exe_prefers_dist(cfg: MeetingConfig) -> None:
    cfg.live_dist_dir.mkdir(parents=True, exist_ok=True)
    dist_exe = cfg.live_dist_exe
    dist_exe.write_text("", encoding="utf-8")
    assert runner.resolve_live_exe(cfg) == dist_exe
    assert runner.needs_live_build(cfg) is False  # publish 成果物は再ビルド対象外


def test_resolve_live_exe_falls_back_to_bin(cfg: MeetingConfig) -> None:
    bin_exe = cfg.live_project_dir / "bin" / "Release" / "net10.0-windows" / LIVE_EXE_NAME
    bin_exe.parent.mkdir(parents=True, exist_ok=True)
    bin_exe.write_text("", encoding="utf-8")
    assert runner.resolve_live_exe(cfg) == bin_exe


def test_resolve_live_exe_none_when_missing(cfg: MeetingConfig) -> None:
    assert runner.resolve_live_exe(cfg) is None
    assert runner.needs_live_build(cfg) is True


class _RecordingRun:
    """subprocess.run 互換の記録用 seam。"""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, cmd: list[str], **kwargs: object) -> "subprocess.CompletedProcess[str]":
        self.calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def test_cmd_live_launches_unit_c_with_injected_paths(cfg: MeetingConfig) -> None:
    # dist に exe を用意 → ビルド不要で前景起動される。
    cfg.live_dist_dir.mkdir(parents=True, exist_ok=True)
    cfg.live_dist_exe.write_text("", encoding="utf-8")
    run = _RecordingRun()

    rc = cli.main(["live"], cfg=cfg, run=run)

    assert rc == 0
    assert len(run.calls) == 1  # ビルドは走らず、live 起動のみ
    cmd, kwargs = run.calls[0]
    assert cmd[0].endswith(LIVE_EXE_NAME)
    env = kwargs["env"]
    assert isinstance(env, dict)
    sink = env["SUBTEXT_Live__CaptionSinkPath"]
    assert sink.endswith("live_captions.jsonl")
    assert str(cfg.out_dir) in sink  # セッションは data/out 配下
    # セッション出力ディレクトリが作られている。
    sessions = [d for d in cfg.out_dir.iterdir() if d.is_dir()]
    assert len(sessions) == 1
