"""runner.py の単体テスト。状態検知・見積・コマンド組立・subprocess seam を検証する。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from meeting import runner
from meeting.config import MeetingConfig
from meeting.runner import Stage

# conftest を直接参照する（tests/ に __init__.py を置かないため、pytest が prepend で
# この tests ディレクトリを sys.path 先頭へ挿入する）。workspace 共有 venv では
# src/postmeeting/tests も path 上に載るため、`tests.conftest` 表記だと名前空間が衝突する。
from conftest import write_manifest, write_out_file

_SESSION = "20260625-120156"


# --- resolve_session ----------------------------------------------------------


@pytest.mark.unit
def test_resolve_session_passthrough(cfg: MeetingConfig) -> None:
    assert runner.resolve_session(cfg, "  S9  ") == "S9"


@pytest.mark.unit
def test_resolve_session_picks_latest(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, "20260101-000000", self_sec=1, others_sec=1)
    write_manifest(repo, "20260625-120156", self_sec=1, others_sec=1)
    assert runner.resolve_session(cfg, None) == "20260625-120156"


@pytest.mark.unit
def test_resolve_session_raises_when_none(cfg: MeetingConfig) -> None:
    with pytest.raises(FileNotFoundError):
        runner.resolve_session(cfg, None)


@pytest.mark.unit
def test_resolve_session_considers_vtt_origin_sessions(cfg: MeetingConfig, repo: Path) -> None:
    """VTTインポート由来（out のみ・manifest 無し。FR-17）も既定セッション解決の母集団に含める。

    recordings だけを見ると、直近作業が VTT 取込のとき古い録音セッションを既定に選び、
    `meeting slack` が別セッションの minutes.md を投稿し得た（回帰防止）。
    """
    write_manifest(repo, "20260101-000000", self_sec=1, others_sec=1)
    write_out_file(repo, "meeting-2026-07-20", "minutes.md", "# 議事録")

    assert runner.resolve_session(cfg, None) == "meeting-2026-07-20"


def _set_mtime(path: Path, epoch: float) -> None:
    """ディレクトリの最終更新時刻を固定する（既定セッション解決の順序を決定的に検証するため）。"""
    os.utime(path, (epoch, epoch))


@pytest.mark.unit
def test_resolve_session_prefers_recently_updated_over_lexicographic_name(
    cfg: MeetingConfig, repo: Path
) -> None:
    """既定セッションは「名前順の最大」ではなく「意味的に新しい方」を選ぶこと。

    セッションIDの名前空間が2系統混在する（録音=`yyyyMMdd-HHmmss` / 取込=ファイル名 stem）ため、
    英字始まりの stem は必ずタイムスタンプより大きい。`live_captions`（ライブ字幕の既定出力名）や
    `Recording`（Teams 既定名）のディレクトリが一度できると、以降 `meeting slack` の既定が
    常にそれになり、**別会議の実名入り議事録を Slack へ投稿**してしまう（取り消せない）。
    """
    write_manifest(repo, "20260729-101500", self_sec=1, others_sec=1)  # 今日の録音（名前は小さい）
    write_out_file(repo, "live_captions", "minutes.md", "# 別会議")  # 古い取込（名前は大きい）
    _set_mtime(repo / "data" / "out" / "live_captions", 1_700_000_000.0)
    _set_mtime(repo / "data" / "recordings" / "20260729-101500", 1_800_000_000.0)

    assert runner.resolve_session(cfg, None) == "20260729-101500"
    # 一覧も同じ順序（新しい順）で返す。
    assert [s.session_id for s in runner.list_sessions(cfg)] == ["20260729-101500", "live_captions"]


# --- list_sessions --------------------------------------------------------------


@pytest.mark.unit
def test_list_sessions_empty_without_recordings_dir(cfg: MeetingConfig) -> None:
    assert runner.list_sessions(cfg) == []


@pytest.mark.unit
def test_list_sessions_orders_newest_first_and_reflects_stage(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, "20260101-000000", self_sec=10, others_sec=10)
    write_manifest(repo, _SESSION, self_sec=120.0, others_sec=118.0)
    write_out_file(repo, _SESSION, "minutes.md", "# 議事録")

    sessions = runner.list_sessions(cfg)

    assert [s.session_id for s in sessions] == [_SESSION, "20260101-000000"]
    done = sessions[0]
    assert done.stage == Stage.MINUTES_DONE
    assert done.status == "complete"
    assert done.self_sec == 120.0
    assert done.others_sec == 118.0
    assert sessions[1].stage == Stage.RECORDED


@pytest.mark.unit
def test_list_sessions_includes_vtt_origin_without_manifest(cfg: MeetingConfig, repo: Path) -> None:
    """VTTインポート由来（FR-17）は data/out のみに実体を持ち manifest が無い。"""
    write_out_file(repo, "meeting-2026-06-01", "speaker_names.json")

    sessions = runner.list_sessions(cfg)

    assert [s.session_id for s in sessions] == ["meeting-2026-06-01"]
    vtt = sessions[0]
    assert vtt.stage == Stage.NAMING_REQUIRED
    assert vtt.status == "vtt"
    assert vtt.self_sec is None
    assert vtt.others_sec is None


@pytest.mark.unit
def test_list_sessions_merges_recorded_and_vtt_origin(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    write_out_file(repo, "20260101-000000", "speaker_names.json")

    sessions = runner.list_sessions(cfg)

    # 両系統が母集団に入ること（並び順の契約は
    # test_resolve_session_prefers_recently_updated_over_lexicographic_name が持つ）。
    assert {s.session_id for s in sessions} == {_SESSION, "20260101-000000"}


# --- has_manifest ---------------------------------------------------------------


@pytest.mark.unit
def test_has_manifest_true_for_recorded_session(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    assert runner.has_manifest(cfg, _SESSION) is True


@pytest.mark.unit
def test_has_manifest_false_for_vtt_origin_session(cfg: MeetingConfig, repo: Path) -> None:
    write_out_file(repo, "meeting-2026-06-01", "speaker_names.json")
    assert runner.has_manifest(cfg, "meeting-2026-06-01") is False


# --- read_manifest ------------------------------------------------------------


@pytest.mark.unit
def test_read_manifest_parses_streams(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=120.4, others_sec=118.0, status="complete")
    m = runner.read_manifest(cfg, _SESSION)
    assert m.session_id == _SESSION
    assert m.status == "complete"
    assert m.self_sec == 120.4
    assert m.others_sec == 118.0


@pytest.mark.unit
def test_read_manifest_missing_raises(cfg: MeetingConfig) -> None:
    with pytest.raises(FileNotFoundError):
        runner.read_manifest(cfg, "nope")


# --- detect_stage -------------------------------------------------------------


@pytest.mark.unit
def test_detect_stage_no_recording(cfg: MeetingConfig) -> None:
    assert runner.detect_stage(cfg, _SESSION) == Stage.NO_RECORDING


@pytest.mark.unit
def test_detect_stage_recorded(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    assert runner.detect_stage(cfg, _SESSION) == Stage.RECORDED


@pytest.mark.unit
def test_detect_stage_naming_required(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    write_out_file(repo, _SESSION, "speaker_names.json")
    assert runner.detect_stage(cfg, _SESSION) == Stage.NAMING_REQUIRED


@pytest.mark.unit
def test_detect_stage_transcript_done(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    write_out_file(repo, _SESSION, "speaker_names.json")
    write_out_file(repo, _SESSION, "final_transcript.json")
    assert runner.detect_stage(cfg, _SESSION) == Stage.TRANSCRIPT_DONE


@pytest.mark.unit
def test_detect_stage_minutes_done(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    write_out_file(repo, _SESSION, "final_transcript.json")
    write_out_file(repo, _SESSION, "minutes.md", "# 議事録")
    assert runner.detect_stage(cfg, _SESSION) == Stage.MINUTES_DONE


@pytest.mark.unit
def test_detect_stage_vtt_origin_without_manifest(cfg: MeetingConfig, repo: Path) -> None:
    """VTTインポート由来（FR-17）は manifest.json が無くても out 側の実体で判定する。"""
    write_out_file(repo, "meeting-2026-06-01", "final_transcript.json")
    write_out_file(repo, "meeting-2026-06-01", "minutes.md", "# 議事録")
    assert runner.detect_stage(cfg, "meeting-2026-06-01") == Stage.MINUTES_DONE


# --- delete_session -------------------------------------------------------------


@pytest.mark.unit
def test_delete_session_removes_both_directories(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    write_out_file(repo, _SESSION, "minutes.md", "# 議事録")

    runner.delete_session(cfg, _SESSION)

    assert not cfg.session_recording_dir(_SESSION).is_dir()
    assert not cfg.session_out_dir(_SESSION).is_dir()


@pytest.mark.unit
def test_delete_session_ignores_missing_directories(cfg: MeetingConfig) -> None:
    runner.delete_session(cfg, "nope")  # 例外を投げずに戻ること


# --- transcript_char_count / estimates ---------------------------------------


@pytest.mark.unit
def test_transcript_char_count_sums_text(cfg: MeetingConfig, repo: Path) -> None:
    transcript = {"segments": [{"text": "あいう"}, {"text": "えお"}]}
    write_out_file(repo, _SESSION, "final_transcript.json", json.dumps(transcript))
    assert runner.transcript_char_count(cfg, _SESSION) == 5


@pytest.mark.unit
def test_estimate_transcribe_uses_manifest(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=120.4, others_sec=118.0)
    expected = round((120.4 + 118.0) / 60.0 * 0.024, 4)
    est = runner.estimate_transcribe(cfg, _SESSION)
    assert est.stage == "transcribe"
    assert est.usd == pytest.approx(expected)


@pytest.mark.unit
def test_materials_char_count_zero_when_no_cache_file(cfg: MeetingConfig, repo: Path) -> None:
    assert runner.materials_char_count(cfg, _SESSION) == 0


@pytest.mark.unit
def test_materials_char_count_sums_included_materials(cfg: MeetingConfig, repo: Path) -> None:
    cache = {
        "materials": [{"fileName": "a.txt", "charCount": 10}, {"fileName": "b.pdf", "charCount": 20}],
        "excluded": [],
        "warnings": [],
    }
    write_out_file(repo, _SESSION, "materials.extracted.json", json.dumps(cache))
    assert runner.materials_char_count(cfg, _SESSION) == 30


@pytest.mark.unit
def test_materials_char_count_zero_when_cache_broken(cfg: MeetingConfig, repo: Path) -> None:
    write_out_file(repo, _SESSION, "materials.extracted.json", "{not json")
    assert runner.materials_char_count(cfg, _SESSION) == 0


@pytest.mark.unit
def test_estimate_summary_includes_materials_chars(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    write_out_file(repo, _SESSION, "final_transcript.json", json.dumps({"segments": [{"text": "x" * 1000}]}))
    without_materials = runner.estimate_summary(cfg, _SESSION, claude=False)

    write_out_file(
        repo, _SESSION, "materials.extracted.json", json.dumps({"materials": [{"charCount": 500}]})
    )
    with_materials = runner.estimate_summary(cfg, _SESSION, claude=False)

    assert with_materials is not None and without_materials is not None
    assert with_materials.usd > without_materials.usd


@pytest.mark.unit
def test_estimate_summary_none_without_transcript(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    assert runner.estimate_summary(cfg, _SESSION, claude=False) is None


@pytest.mark.unit
def test_estimate_summary_claude_marks_pii(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    write_out_file(repo, _SESSION, "final_transcript.json", json.dumps({"segments": [{"text": "x" * 1000}]}))
    est = runner.estimate_summary(cfg, _SESSION, claude=True)
    assert est is not None
    assert est.stage == "claude"
    assert est.pii_sent is True


# --- build commands / needs_build --------------------------------------------


@pytest.mark.unit
def test_build_pipeline_command_default(cfg: MeetingConfig) -> None:
    cmd = runner.build_pipeline_command(cfg, _SESSION, claude=False)
    assert cmd[:5] == ["uv", "run", "subtext-postmeeting", "--mode", "paired"]
    assert "--no-summarize" not in cmd


# --- resolve_postmeeting_cmd（uv 一本） ---------------------------------------


@pytest.mark.unit
def test_resolve_postmeeting_cmd_uses_uv(cfg: MeetingConfig) -> None:
    # Unit B に dist 経路は無い。TUI 自体が uv 起動のため配布先でも uv run に乗る。
    assert runner.resolve_postmeeting_cmd() == ["uv", "run", "subtext-postmeeting"]


@pytest.mark.unit
def test_build_pipeline_command_claude_adds_no_summarize(cfg: MeetingConfig) -> None:
    cmd = runner.build_pipeline_command(cfg, _SESSION, claude=True)
    assert cmd[-1] == "--no-summarize"


# --- VTTインポート（FR-17） -----------------------------------------------------


@pytest.mark.unit
def test_vtt_session_id_uses_filename_stem() -> None:
    assert runner.vtt_session_id(Path("/tmp/meeting-2026-06-01.vtt")) == "meeting-2026-06-01"


@pytest.mark.unit
def test_build_vtt_pipeline_command_default(cfg: MeetingConfig) -> None:
    cmd = runner.build_vtt_pipeline_command(cfg, Path("meeting.vtt"), claude=False)
    assert cmd == ["uv", "run", "subtext-postmeeting", "--mode", "vtt", "--vtt", "meeting.vtt"]


@pytest.mark.unit
def test_build_vtt_pipeline_command_claude_adds_no_summarize(cfg: MeetingConfig) -> None:
    cmd = runner.build_vtt_pipeline_command(cfg, Path("meeting.vtt"), claude=True)
    assert cmd[-1] == "--no-summarize"


@pytest.mark.unit
def test_launch_vtt_pipeline_injects_output_dir_env(cfg: MeetingConfig) -> None:
    calls: list[dict] = []

    def fake_runner(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"cmd": cmd, "kwargs": kwargs})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = runner.launch_vtt_pipeline(cfg, Path("meeting.vtt"), claude=False, runner=fake_runner)

    assert result.returncode == 0
    assert calls[0]["kwargs"]["cwd"] == str(cfg.repo_root)
    assert calls[0]["kwargs"]["env"]["OUTPUT_DIR"] == str(cfg.out_dir)
    assert "--vtt" in calls[0]["cmd"]


# --- mp4 取込（FR-18） -----------------------------------------------------------


@pytest.mark.unit
def test_resolve_mp4_to_vtt_cmd_uses_uv() -> None:
    assert runner.resolve_mp4_to_vtt_cmd() == ["uv", "run", "subtext-mp4-to-vtt"]


@pytest.mark.unit
def test_mp4_vtt_output_path_sits_next_to_input(cfg: MeetingConfig) -> None:
    """出力は入力と同じ場所の `.vtt`（subtext-mp4-to-vtt CLI の --out 既定と同一）。"""
    assert runner.mp4_vtt_output_path(Path("/rec/会議 2026-06-01.mp4")) == Path("/rec/会議 2026-06-01.vtt")


@pytest.mark.unit
def test_mp4_vtt_output_path_session_id_matches_vtt_route(cfg: MeetingConfig) -> None:
    """mp4 と生成 VTT のセッションIDが一致すること（台帳の段が同じ session に並ぶ前提）。"""
    mp4 = Path("/rec/meeting-2026-06-01.mp4")
    assert runner.vtt_session_id(mp4) == runner.vtt_session_id(runner.mp4_vtt_output_path(mp4))


@pytest.mark.unit
def test_build_mp4_to_vtt_command_passes_explicit_out(cfg: MeetingConfig) -> None:
    cmd = runner.build_mp4_to_vtt_command(cfg, Path("rec.mp4"), Path("rec.vtt"))
    assert cmd == ["uv", "run", "subtext-mp4-to-vtt", "rec.mp4", "--out", "rec.vtt"]


@pytest.mark.unit
def test_probe_duration_sec_parses_ffprobe_output() -> None:
    def fake_runner(cmd, **kwargs):  # type: ignore[no-untyped-def]
        assert cmd[0] == "ffprobe"
        return SimpleNamespace(returncode=0, stdout="1234.56\n", stderr="")

    assert runner.probe_duration_sec(Path("rec.mp4"), runner=fake_runner) == 1234.56


@pytest.mark.unit
def test_probe_duration_sec_returns_none_when_ffprobe_missing() -> None:
    """ffprobe 未導入（FileNotFoundError）は None に畳む（呼び出し側が中止を判断する）。"""

    def fake_runner(cmd, **kwargs):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("ffprobe")

    assert runner.probe_duration_sec(Path("rec.mp4"), runner=fake_runner) is None


@pytest.mark.unit
def test_probe_duration_sec_returns_none_on_nonzero_exit() -> None:
    def fake_runner(cmd, **kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(returncode=1, stdout="", stderr="invalid data")

    assert runner.probe_duration_sec(Path("rec.mp4"), runner=fake_runner) is None


@pytest.mark.unit
def test_probe_duration_sec_returns_none_on_unparsable_output() -> None:
    """`N/A` を返すコンテナもあるため、数値化できない出力は None に畳む。"""

    def fake_runner(cmd, **kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(returncode=0, stdout="N/A\n", stderr="")

    assert runner.probe_duration_sec(Path("rec.mp4"), runner=fake_runner) is None


@pytest.mark.unit
def test_probe_duration_sec_returns_none_on_zero_duration() -> None:
    def fake_runner(cmd, **kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(returncode=0, stdout="0.0\n", stderr="")

    assert runner.probe_duration_sec(Path("rec.mp4"), runner=fake_runner) is None


@pytest.mark.unit
def test_launch_mp4_to_vtt_runs_at_repo_root(cfg: MeetingConfig) -> None:
    """.env をルートから拾わせるため cwd=repo_root で起動すること。"""
    calls: list[dict] = []

    def fake_runner(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"cmd": cmd, "kwargs": kwargs})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = runner.launch_mp4_to_vtt(cfg, Path("rec.mp4"), Path("rec.vtt"), runner=fake_runner)

    assert result.returncode == 0
    assert calls[0]["kwargs"]["cwd"] == str(cfg.repo_root)
    assert calls[0]["cmd"][-2:] == ["--out", "rec.vtt"]


# dev ビルド出力を模した exe を bin 配下へ作る。TargetFramework 名は任意（glob 探索で
# 解決するため）— 実装が構成/TFM を直書きしないことを neutral な階層名で担保する。
def _make_bin_exe(cfg: MeetingConfig) -> Path:
    exe = cfg.recorder_project_dir / "bin" / "Release" / "net-any" / "Subtext.Recorder.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("exe", encoding="utf-8")
    return exe


@pytest.mark.unit
def test_build_recorder_command(cfg: MeetingConfig, tmp_path: Path) -> None:
    exe = tmp_path / "Subtext.Recorder.exe"
    cmd = runner.build_recorder_command(cfg, exe, 30)
    assert cmd[0] == str(exe)
    assert "--minutes" in cmd and "30" in cmd
    assert "--stop-file" in cmd
    assert "--out" in cmd


@pytest.mark.unit
def test_resolve_recorder_exe_prefers_dist(cfg: MeetingConfig) -> None:
    _make_bin_exe(cfg)  # dev ビルドがあっても
    cfg.recorder_dist_exe.parent.mkdir(parents=True, exist_ok=True)
    cfg.recorder_dist_exe.write_text("dist-exe", encoding="utf-8")
    assert runner.resolve_recorder_exe(cfg) == cfg.recorder_dist_exe


@pytest.mark.unit
def test_needs_recorder_build_when_exe_missing(cfg: MeetingConfig) -> None:
    assert runner.needs_recorder_build(cfg) is True


@pytest.mark.unit
def test_needs_recorder_build_false_when_exe_newer(cfg: MeetingConfig) -> None:
    cfg.recorder_csproj.parent.mkdir(parents=True, exist_ok=True)
    cfg.recorder_csproj.write_text("<Project/>", encoding="utf-8")
    exe = _make_bin_exe(cfg)
    # exe を csproj より新しくする。
    os.utime(cfg.recorder_csproj, (1000, 1000))
    os.utime(exe, (2000, 2000))
    assert runner.needs_recorder_build(cfg) is False


@pytest.mark.unit
def test_needs_recorder_build_true_when_csproj_newer(cfg: MeetingConfig) -> None:
    cfg.recorder_csproj.parent.mkdir(parents=True, exist_ok=True)
    cfg.recorder_csproj.write_text("<Project/>", encoding="utf-8")
    exe = _make_bin_exe(cfg)
    os.utime(exe, (1000, 1000))
    os.utime(cfg.recorder_csproj, (2000, 2000))
    assert runner.needs_recorder_build(cfg) is True


@pytest.mark.unit
def test_needs_recorder_build_false_when_dist_exists(cfg: MeetingConfig) -> None:
    # publish 成果物は csproj の新旧に関わらず再ビルド判定の対象外。
    cfg.recorder_csproj.parent.mkdir(parents=True, exist_ok=True)
    cfg.recorder_csproj.write_text("<Project/>", encoding="utf-8")
    cfg.recorder_dist_exe.parent.mkdir(parents=True, exist_ok=True)
    cfg.recorder_dist_exe.write_text("dist-exe", encoding="utf-8")
    os.utime(cfg.recorder_dist_exe, (1000, 1000))
    os.utime(cfg.recorder_csproj, (2000, 2000))
    assert runner.needs_recorder_build(cfg) is False


# --- run_pipeline seam --------------------------------------------------------


@pytest.mark.unit
def test_run_pipeline_invokes_runner_with_cwd(cfg: MeetingConfig) -> None:
    calls: list[dict] = []

    def fake_runner(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"cmd": cmd, "kwargs": kwargs})
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    result = runner.run_pipeline(cfg, _SESSION, claude=True, runner=fake_runner)

    assert result.returncode == 0
    # workspace 化により cwd はリポジトリルート（cd 不要）。
    assert calls[0]["kwargs"]["cwd"] == str(cfg.repo_root)
    # 出力先は OUTPUT_DIR=data/out を環境注入して段検知と一致させる。
    assert calls[0]["kwargs"]["env"]["OUTPUT_DIR"] == str(cfg.out_dir)
    assert calls[0]["cmd"][-1] == "--no-summarize"


# --- build_ledger_entry -------------------------------------------------------


@pytest.mark.unit
def test_build_ledger_entry_transcribe(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=120.0, others_sec=120.0)
    est = runner.estimate_transcribe(cfg, _SESSION)
    entry = runner.build_ledger_entry(
        _SESSION,
        est,
        unit_price_usd=0.024,
        cumulative_month_usd=est.usd,
        units={"minutes": 4.0},
        ts="2026-06-25T12:30:00+00:00",
    )
    assert entry.backend == "aws"
    assert entry.pii_sent is False
    assert entry.stage == "transcribe"


@pytest.mark.unit
def test_build_ledger_entry_claude_is_pii(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=1, others_sec=1)
    write_out_file(repo, _SESSION, "final_transcript.json", json.dumps({"segments": [{"text": "x" * 500}]}))
    est = runner.estimate_summary(cfg, _SESSION, claude=True)
    entry = runner.build_ledger_entry(
        _SESSION,
        est,
        unit_price_usd=0.003,
        cumulative_month_usd=est.usd,
        units={"chars": 500},
        ts="2026-06-25T12:30:00+00:00",
    )
    assert entry.backend == "claude"
    assert entry.pii_sent is True


# --- AWS 資格情報の疎通確認（FR-H2-11） ---------------------------------------


@pytest.mark.unit
def test_build_check_auth_command_uses_unit_b() -> None:
    # ハーネスは boto3 を持たず Unit B の分類も import できない（FR-16）。確認はプロセス起動で行う。
    assert runner.build_check_auth_command() == ["uv", "run", "subtext-postmeeting", "--check-auth"]


@pytest.mark.unit
def test_run_check_auth_parses_machine_readable_lines(cfg: MeetingConfig) -> None:
    calls: list[dict] = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": list(cmd), "kwargs": kwargs})
        return SimpleNamespace(
            returncode=0,
            stdout="authProfile=default\nauthRegion=ap-northeast-1\nauthStatus=ok\n",
            stderr="",
        )

    status = runner.run_check_auth(cfg, runner=fake_run)

    assert status.ok
    assert (status.profile, status.region) == ("default", "ap-northeast-1")
    assert status.message == ""
    # 応答しない環境で操作を待たせないため待ち上限を付ける。
    assert calls[0]["kwargs"]["timeout"] == runner.CHECK_AUTH_TIMEOUT_SEC
    assert calls[0]["kwargs"]["cwd"] == str(cfg.repo_root)


@pytest.mark.unit
def test_run_check_auth_keeps_remediation_message(cfg: MeetingConfig) -> None:
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout=(
                "authProfile=subtext\nauthRegion=ap-northeast-1\nauthStatus=expired\n"
                "AWS 認証が無効または期限切れです。`! aws login` で再認証してください。\n"
            ),
            stderr="",
        )

    status = runner.run_check_auth(cfg, runner=fake_run)

    assert status.state is runner.AuthState.EXPIRED
    assert status.label == "認証切れ"
    assert "aws login" in status.message  # 対処メッセージの正本は Unit B 側


@pytest.mark.unit
def test_run_check_auth_timeout_is_reported_as_check_failure(cfg: MeetingConfig) -> None:
    import subprocess

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, timeout=1)

    status = runner.run_check_auth(cfg, runner=fake_run)

    assert status.state is runner.AuthState.ERROR
    assert not status.ok
    assert "応答しませんでした" in status.message


@pytest.mark.unit
def test_run_check_auth_launch_failure_does_not_raise(cfg: MeetingConfig) -> None:
    # 起動時に必ず走る経路のため、uv 不在でも例外で TUI を落とさない。
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("uv")

    status = runner.run_check_auth(cfg, runner=fake_run)

    assert status.state is runner.AuthState.ERROR
    assert "起動できませんでした" in status.message


@pytest.mark.unit
def test_parse_auth_output_without_status_is_not_success() -> None:
    """`authStatus=` が無い出力を成功と読み違えない（Unit B が別の理由で落ちた場合）。"""
    status = runner.parse_auth_output("", stderr="ERROR: S3_BUCKET が設定されていません。")

    assert status.state is runner.AuthState.ERROR
    assert "S3_BUCKET" in status.message


@pytest.mark.unit
def test_parse_auth_output_unknown_status_is_not_success() -> None:
    # Unit B 側に新しい分類が増えても成功扱いにしない。
    status = runner.parse_auth_output("authStatus=brand_new_kind\n")

    assert status.state is runner.AuthState.UNKNOWN


# --- AWS プロファイルの注入（TODO: .env / --profile 指定） ------------------------


@pytest.mark.unit
def test_aws_profile_env_is_empty_when_unspecified(cfg: MeetingConfig) -> None:
    """未指定なら AWS_PROFILE を渡さない。

    `AWS_PROFILE=default` を入れると、`~/.aws/config` に `[default]` が無い環境
    （資格情報を環境変数で渡す運用・CI）で botocore が ProfileNotFound を投げる。
    """
    assert cfg.aws_profile == ""
    assert runner.aws_profile_env(cfg) == {}


@pytest.mark.unit
def test_aws_profile_env_passes_resolved_profile(cfg: MeetingConfig) -> None:
    from dataclasses import replace

    assert runner.aws_profile_env(replace(cfg, aws_profile="subtext-dev")) == {"AWS_PROFILE": "subtext-dev"}


@pytest.mark.unit
def test_pipeline_child_receives_profile(cfg: MeetingConfig) -> None:
    from dataclasses import replace

    calls: list[dict] = []

    def fake_run(cmd, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner.run_pipeline(replace(cfg, aws_profile="subtext-dev"), "20260625-120156", claude=False, runner=fake_run)

    assert calls[0]["env"]["AWS_PROFILE"] == "subtext-dev"


@pytest.mark.unit
def test_pipeline_child_has_no_profile_when_unspecified(cfg: MeetingConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    calls: list[dict] = []

    def fake_run(cmd, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner.run_pipeline(cfg, "20260625-120156", claude=False, runner=fake_run)

    assert "AWS_PROFILE" not in calls[0]["env"]


@pytest.mark.unit
def test_live_env_carries_profile(cfg: MeetingConfig) -> None:
    """Unit C は .env を読まないため、注入しないとライブ字幕だけ別プロファイルで動く。"""
    from dataclasses import replace

    env = runner.build_live_env(replace(cfg, aws_profile="subtext-dev"), "20260625-120156")

    assert env["AWS_PROFILE"] == "subtext-dev"
    assert "SUBTEXT_Live__CaptionSinkPath" in env  # 既存の注入を壊していない


@pytest.mark.unit
def test_ledger_entry_records_profile(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=60.0, others_sec=60.0)
    est = runner.estimate_transcribe(cfg, _SESSION)

    entry = runner.build_ledger_entry(
        _SESSION,
        est,
        unit_price_usd=0.024,
        cumulative_month_usd=est.usd,
        units={"minutes": 2.0},
        ts="2026-08-04T12:00:00+00:00",
        profile="subtext-dev",
    )

    assert entry.profile == "subtext-dev"
    assert entry.to_json_obj()["profile"] == "subtext-dev"


@pytest.mark.unit
def test_parse_auth_output_maps_profile_not_found() -> None:
    """プロファイル不在は「不明」ではなく専用の表示にする（対処が違うため）。"""
    status = runner.parse_auth_output(
        "authProfile=typo\nauthRegion=ap-northeast-1\nauthStatus=profile_not_found\n綴りを確認してください。\n"
    )

    assert status.state is runner.AuthState.PROFILE_NOT_FOUND
    assert status.label == "プロファイル不明"
    assert status.profile == "typo"


@pytest.mark.unit
def test_parse_auth_output_prefers_tail_of_stderr() -> None:
    """原因は stderr の末尾に出る（`uv run` が解決/インストールの進捗を先に書くため）。"""
    stderr = (
        "Resolved 45 packages in 1.2s\n"
        "Prepared 3 packages in 0.8s\n"
        "Installed 3 packages in 12ms\n"
        " + boto3==1.40.0\n"
        " + botocore==1.43.45\n"
        "ERROR subtext.postmeeting: パイプライン失敗 S3_BUCKET が設定されていません。\n"
    )

    status = runner.parse_auth_output("", stderr=stderr)

    assert status.state is runner.AuthState.ERROR
    assert "S3_BUCKET" in status.message
    assert "Resolved 45 packages" not in status.message


@pytest.mark.unit
def test_parse_auth_output_keeps_indented_remediation_lines() -> None:
    """対処メッセージは複数行で来る（折り返さないログでも読めるように字下げごと残す）。"""
    stdout = (
        "authProfile=typo\nauthStatus=profile_not_found\n"
        "指定された AWS プロファイルが見つかりません。\n"
        "  綴りを確認してください。\n"
    )

    status = runner.parse_auth_output(stdout)

    assert status.message.splitlines() == [
        "指定された AWS プロファイルが見つかりません。",
        "  綴りを確認してください。",
    ]
