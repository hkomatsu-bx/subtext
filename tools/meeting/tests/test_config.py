"""config.py の単体テスト（パス解決・リポジトリルート探索・環境変数尊重）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from meeting.config import MeetingConfig, find_repo_root, load_config

_MEETING_TOML = Path(__file__).resolve().parent.parent / "meeting.toml"


@pytest.mark.unit
def test_find_repo_root_walks_up(repo: Path) -> None:
    nested = repo / "src" / "postmeeting"
    assert find_repo_root(nested) == repo


@pytest.mark.unit
def test_find_repo_root_raises_when_absent(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        find_repo_root(tmp_path)


@pytest.mark.unit
def test_load_config_resolves_paths(cfg: MeetingConfig, repo: Path) -> None:
    assert cfg.repo_root == repo
    assert cfg.recordings_dir == repo / "data" / "recordings"
    assert cfg.out_dir == repo / "data" / "out"
    assert cfg.stop_file == repo / "data" / "recordings" / ".stop"
    assert cfg.session_out_dir("S1") == repo / "data" / "out" / "S1"
    # recorder のプロジェクト/publish 位置も [paths] 既定から解決される。
    assert cfg.recorder_csproj == repo / "src" / "recorder" / "Subtext.Recorder.csproj"
    assert cfg.recorder_dist_exe == repo / "dist" / "recorder" / "Subtext.Recorder.exe"
    # Unit B は dist 経路を持たない（uv run 一本）。
    assert cfg.postmeeting_dir == repo / "src" / "postmeeting"


@pytest.mark.unit
def test_load_config_paths_overridable_via_toml(repo: Path, tmp_path: Path) -> None:
    """レイアウトは [paths] で上書きでき、未指定キーは既定へフォールバックする。"""
    base = _MEETING_TOML.read_text(encoding="utf-8")
    assert 'out_dir = "data/out"' in base  # 前提: 実 toml の既定値
    custom_toml = tmp_path / "meeting.toml"
    custom_toml.write_text(
        base.replace('out_dir = "data/out"', 'out_dir = "artifacts/minutes"'),
        encoding="utf-8",
    )
    cfg = load_config(meeting_toml=custom_toml, start_dir=repo, env={})
    assert cfg.out_dir == repo / "artifacts" / "minutes"  # 上書きが効く
    assert cfg.recordings_dir == repo / "data" / "recordings"  # 未指定は既定


@pytest.mark.unit
def test_load_config_ledger_override(repo: Path) -> None:
    cfg = load_config(
        meeting_toml=_MEETING_TOML,
        start_dir=repo,
        env={"SUBTEXT_MEETING_LEDGER": str(repo / "x.jsonl")},
    )
    assert cfg.ledger_path == repo / "x.jsonl"


@pytest.mark.unit
def test_load_config_pricing_and_thresholds_loaded(cfg: MeetingConfig) -> None:
    assert cfg.pricing.transcribe_usd_per_minute > 0
    assert cfg.thresholds.per_run_usd > 0
    assert cfg.thresholds.monthly_usd > 0
