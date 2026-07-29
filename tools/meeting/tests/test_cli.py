"""cli.py のスモーク/結合テスト。subprocess は seam でモックし課金を起こさない。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from meeting import ledger
from meeting.cli import main
from meeting.config import MeetingConfig

# conftest を直接参照する（詳細は test_runner.py の同 import の注記を参照）。
from conftest import write_manifest, write_out_file, write_pipeline_outputs

_SESSION = "20260625-120156"


def _ok_runner(side_effect=None):
    """returncode=0 を返すダミー runner。side_effect で擬似的に出力ファイルを作る。"""

    def _run(cmd, **kwargs):
        if side_effect is not None:
            side_effect(cmd, kwargs)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    return _run


@pytest.mark.integration
def test_status_reports_recorded(cfg: MeetingConfig, repo: Path, capsys) -> None:
    write_manifest(repo, _SESSION, self_sec=120.0, others_sec=118.0)
    rc = main(["status", _SESSION], cfg=cfg, run=_ok_runner())
    out = capsys.readouterr().out
    assert rc == 0
    assert _SESSION in out
    assert "録音済" in out


@pytest.mark.integration
def test_stop_creates_stop_file(cfg: MeetingConfig, capsys) -> None:
    rc = main(["stop"], cfg=cfg, run=_ok_runner())
    assert rc == 0
    assert cfg.stop_file.is_file()


@pytest.mark.integration
def test_cost_empty_ledger(cfg: MeetingConfig, capsys) -> None:
    rc = main(["cost", "--month", "2026-06"], cfg=cfg, run=_ok_runner())
    out = capsys.readouterr().out
    assert rc == 0
    assert "コスト概算サマリ" in out


@pytest.mark.integration
def test_minutes_no_recording_errors(cfg: MeetingConfig, capsys) -> None:
    rc = main(["minutes", "nope"], cfg=cfg, run=_ok_runner())
    assert rc == 1


@pytest.mark.integration
def test_minutes_on_vtt_session_errors_without_crashing(cfg: MeetingConfig, repo: Path, capsys) -> None:
    """VTTインポート由来（FR-17。manifest 無し）に paired 経路の `meeting minutes` を実行すると、
    クラッシュせず actionable なエラーで停止すること（実クラッシュの再発防止）。
    """
    write_out_file(repo, "meeting-2026-06-01", "speaker_names.json")

    rc = main(["minutes", "meeting-2026-06-01"], cfg=cfg, run=_ok_runner())

    assert rc == 1
    err = capsys.readouterr().err
    assert "VTTインポート由来" in err


@pytest.mark.integration
def test_minutes_claude_records_transcribe_and_summary(cfg: MeetingConfig, repo: Path, capsys) -> None:
    write_manifest(repo, _SESSION, self_sec=120.0, others_sec=120.0)

    # パイプライン実行で Unit B の成果物が生成された体にする（Claude 経路は minutes.md を作らない）。
    def side_effect(cmd, kwargs):
        write_pipeline_outputs(repo, _SESSION, minutes=False)

    rc = main(["minutes", _SESSION, "--claude"], cfg=cfg, run=_ok_runner(side_effect))
    assert rc == 0

    entries = ledger.load(cfg.ledger_path)
    stages = {e.stage for e in entries}
    assert "transcribe" in stages
    assert "claude" in stages
    # Claude 経路は PII 送信を記録。
    assert any(e.pii_sent for e in entries if e.stage == "claude")


@pytest.mark.integration
def test_minutes_rerun_does_not_double_count(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=120.0, others_sec=120.0)

    def side_effect(cmd, kwargs):
        write_pipeline_outputs(repo, _SESSION, minutes=False)

    main(["minutes", _SESSION, "--claude"], cfg=cfg, run=_ok_runner(side_effect))
    main(["minutes", _SESSION, "--claude"], cfg=cfg, run=_ok_runner(side_effect))

    entries = ledger.load(cfg.ledger_path)
    # 2回実行しても transcribe / claude は各1件のまま。
    assert sum(1 for e in entries if e.stage == "transcribe") == 1
    assert sum(1 for e in entries if e.stage == "claude") == 1


def _make_bin_exe(cfg: MeetingConfig) -> Path:
    """dev ビルド出力を模した exe（TFM 名は任意＝glob 解決に依存させない）。"""
    exe = cfg.recorder_project_dir / "bin" / "Release" / "net-any" / "Subtext.Recorder.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("exe", encoding="utf-8")
    return exe


@pytest.mark.integration
def test_record_builds_when_exe_missing(cfg: MeetingConfig, capsys) -> None:
    cmds: list[list[str]] = []

    def run(cmd, **kwargs):
        cmds.append(cmd)
        # dotnet build がビルド出力（exe）を生成する体にする（以降 resolve できる）。
        if cmd[:2] == ["dotnet", "build"]:
            _make_bin_exe(cfg)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    rc = main(["record", "30"], cfg=cfg, run=run)
    assert rc == 0
    # exe 未生成 → 先に dotnet build、その後 recorder 起動の2回。
    assert cmds[0][:2] == ["dotnet", "build"]
    assert any("--minutes" in c for c in cmds)


@pytest.mark.integration
def test_record_skips_build_when_exe_fresh(cfg: MeetingConfig) -> None:
    import os

    cfg.recorder_csproj.parent.mkdir(parents=True, exist_ok=True)
    cfg.recorder_csproj.write_text("<Project/>", encoding="utf-8")
    exe = _make_bin_exe(cfg)
    os.utime(cfg.recorder_csproj, (1000, 1000))
    os.utime(exe, (2000, 2000))

    cmds: list[list[str]] = []

    def run(cmd, **kwargs):
        cmds.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    rc = main(["record"], cfg=cfg, run=run)
    assert rc == 0
    # ビルドは走らず recorder 起動のみ。
    assert all(c[:2] != ["dotnet", "build"] for c in cmds)
    assert any("--minutes" in c for c in cmds)


@pytest.mark.integration
def test_no_command_prints_help(cfg: MeetingConfig) -> None:
    assert main([], cfg=cfg, run=_ok_runner()) == 2


@pytest.mark.integration
def test_minutes_warns_on_threshold(cfg: MeetingConfig, repo: Path, capsys) -> None:
    # 上限を 0 に潰した cfg を作って必ず警告させる。
    from dataclasses import replace
    from meeting.ledger import Thresholds

    tight = replace(cfg, thresholds=Thresholds(per_run_usd=0.0, monthly_usd=0.0))
    write_manifest(repo, _SESSION, self_sec=120.0, others_sec=120.0)

    def side_effect(cmd, kwargs):
        write_pipeline_outputs(repo, _SESSION)

    rc = main(["minutes", _SESSION], cfg=tight, run=_ok_runner(side_effect))
    out = capsys.readouterr().out
    assert rc == 0
    assert "コスト警告" in out
