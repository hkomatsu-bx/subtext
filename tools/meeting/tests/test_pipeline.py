"""pipeline.py の単体テスト。emit seam・台帳連携・next_steps_lines の純粋ロジックを検証する。"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from meeting import ledger, pipeline
from meeting.config import MeetingConfig
from meeting.runner import Stage

# conftest を直接参照する（詳細は test_runner.py の同 import の注記を参照）。
from conftest import write_manifest, write_out_file, write_pipeline_outputs

_SESSION = "20260625-120156"


def _ok_runner(side_effect=None):
    def _run(cmd, **kwargs):
        if side_effect is not None:
            side_effect(cmd, kwargs)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    return _run


@pytest.mark.unit
def test_run_minutes_pipeline_emits_estimate_and_records_ledger(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=120.0, others_sec=120.0)

    def side_effect(cmd, kwargs):
        write_pipeline_outputs(repo, _SESSION, minutes=False)  # Claude 経路は minutes.md を作らない

    lines: list[str] = []
    rc = pipeline.run_minutes_pipeline(cfg, _SESSION, claude=True, run=_ok_runner(side_effect), emit=lines.append)

    assert rc == 0
    assert any("議事録パイプライン" in line for line in lines)
    entries = ledger.load(cfg.ledger_path)
    assert {"transcribe", "claude"} == {e.stage for e in entries}


@pytest.mark.unit
def test_run_minutes_pipeline_emits_stderr_detail_on_failure(cfg: MeetingConfig, repo: Path) -> None:
    """課金前に落ちた実行（成果物ゼロ）では台帳へ何も積まないこと。

    「異常終了でも記録する」の裏返しの規則。痕跡（raw_transcribe 等）が無い＝Transcribe に
    入っていないため、ここで計上すると幻の金額で月次累計と閾値警告を汚す。課金後に落ちた
    場合は逆に必ず記録する（test_run_minutes_pipeline_records_transcribe_when_failing_after_billing）。
    """
    write_manifest(repo, _SESSION, self_sec=120.0, others_sec=120.0)

    def failing_runner(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    lines: list[str] = []
    rc = pipeline.run_minutes_pipeline(cfg, _SESSION, claude=False, run=failing_runner, emit=lines.append)

    assert rc == 1
    assert any("boom" in line for line in lines)
    assert ledger.load(cfg.ledger_path) == []


@pytest.mark.unit
def test_run_minutes_pipeline_records_transcribe_when_failing_after_billing(
    cfg: MeetingConfig, repo: Path
) -> None:
    """課金後に落ちた実行でも Transcribe を台帳へ残すこと（台帳必須が自動課金の前提）。

    例: others の Transcribe が完了（課金済み）した後に self がタイムアウトして非ゼロ終了。
    成果物（raw_transcribe.*.json）が残るため課金の発生は痕跡から判定できる。ここを記録しないと
    90 分の paired 録音＝180 課金分＝約 $4.32 が台帳ゼロ行・月次累計への寄与ゼロで消える。
    """
    write_manifest(repo, _SESSION, self_sec=2700.0, others_sec=2700.0)

    def failing_runner(cmd, **kwargs):
        write_out_file(repo, _SESSION, "raw_transcribe.others.json", "{}")  # others は課金済み
        return SimpleNamespace(returncode=1, stdout="", stderr="Transcribe ジョブがタイムアウトしました")

    lines: list[str] = []
    rc = pipeline.run_minutes_pipeline(cfg, _SESSION, claude=False, run=failing_runner, emit=lines.append)

    assert rc == 1
    entries = ledger.load(cfg.ledger_path)
    assert [e.stage for e in entries] == ["transcribe"]
    assert entries[0].est_usd > 0
    assert any("Cost Explorer" in line for line in lines), "台帳が欠け得ることを操作者に知らせること"


@pytest.mark.unit
def test_run_minutes_pipeline_records_c2_correction_separately(cfg: MeetingConfig, repo: Path) -> None:
    """C2 用語補正段（Bedrock）を要約段とは別に計上すること。

    既定経路（Bedrock 生成）は要約の前に補正でも全文を Bedrock へ送る。数えないと 1 回の実行で
    発生する Bedrock 支出の約半分が見積・台帳・閾値判定から漏れる。
    """
    write_manifest(repo, _SESSION, self_sec=120.0, others_sec=120.0)

    def side_effect(cmd, kwargs):
        write_pipeline_outputs(repo, _SESSION, chars=20000, correction_status="applied")

    rc = pipeline.run_minutes_pipeline(cfg, _SESSION, claude=False, run=_ok_runner(side_effect), emit=lambda _: None)

    assert rc == 0
    entries = ledger.load(cfg.ledger_path)
    assert {e.stage for e in entries} == {"transcribe", "correct", "bedrock"}
    correct = next(e for e in entries if e.stage == "correct")
    assert correct.est_usd > 0
    assert correct.pii_sent is False  # 補正は Bedrock（自社内）＝PII 外部送信ではない


@pytest.mark.unit
def test_correction_cost_not_recorded_when_stage_skipped(cfg: MeetingConfig, repo: Path) -> None:
    """用語リストが空（status=skipped）なら Bedrock を呼んでいないため計上しないこと。"""
    write_manifest(repo, _SESSION, self_sec=120.0, others_sec=120.0)

    def side_effect(cmd, kwargs):
        write_pipeline_outputs(repo, _SESSION, correction_status="skipped")

    pipeline.run_minutes_pipeline(cfg, _SESSION, claude=False, run=_ok_runner(side_effect), emit=lambda _: None)

    assert not any(e.stage == "correct" for e in ledger.load(cfg.ledger_path))


@pytest.mark.unit
def test_record_costs_skips_already_recorded_stage(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=120.0, others_sec=120.0)
    write_pipeline_outputs(repo, _SESSION)
    month = ledger.month_of(ledger.now_iso())

    first = pipeline.record_costs(cfg, _SESSION, False, month)
    assert first > 0

    second = pipeline.record_costs(cfg, _SESSION, False, month)
    assert second == 0.0
    assert sum(1 for e in ledger.load(cfg.ledger_path) if e.stage == "transcribe") == 1


# --- run_vtt_pipeline / record_summary_cost（FR-17: VTTインポート） -------------


@pytest.mark.unit
def test_run_vtt_pipeline_records_only_summary_cost(cfg: MeetingConfig, repo: Path) -> None:
    vtt_path = repo / "meeting-2026-06-01.vtt"
    vtt_path.write_text("WEBVTT\n", encoding="utf-8")

    def side_effect(cmd, kwargs):
        write_pipeline_outputs(repo, "meeting-2026-06-01", raw=False)  # 取込経路は Transcribe を経ない

    lines: list[str] = []
    session_id, rc = pipeline.run_vtt_pipeline(
        cfg, vtt_path, claude=False, run=_ok_runner(side_effect), emit=lines.append
    )

    assert session_id == "meeting-2026-06-01"
    assert rc == 0
    assert any("VTTインポート" in line for line in lines)
    entries = ledger.load(cfg.ledger_path)
    # Transcribe を経由しないため要約段のみが記録される。
    assert {e.stage for e in entries} == {"bedrock"}


@pytest.mark.unit
def test_run_vtt_pipeline_records_correction_cost(cfg: MeetingConfig, repo: Path) -> None:
    """取込経路（FR-17）でも既定は補正段で Bedrock を踏むため、補正と要約の 2 行を記録すること。"""
    vtt_path = repo / "meeting-2026-06-01.vtt"
    vtt_path.write_text("WEBVTT\n", encoding="utf-8")

    def side_effect(cmd, kwargs):
        write_pipeline_outputs(repo, "meeting-2026-06-01", raw=False, correction_status="applied")

    _, rc = pipeline.run_vtt_pipeline(cfg, vtt_path, claude=False, run=_ok_runner(side_effect), emit=lambda _: None)

    assert rc == 0
    assert {e.stage for e in ledger.load(cfg.ledger_path)} == {"correct", "bedrock"}


@pytest.mark.unit
def test_run_vtt_pipeline_emits_stderr_detail_on_failure(cfg: MeetingConfig, repo: Path) -> None:
    vtt_path = repo / "meeting.vtt"
    vtt_path.write_text("WEBVTT\n", encoding="utf-8")

    def failing_runner(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    lines: list[str] = []
    session_id, rc = pipeline.run_vtt_pipeline(cfg, vtt_path, claude=False, run=failing_runner, emit=lines.append)

    assert session_id == "meeting"
    assert rc == 1
    assert any("boom" in line for line in lines)
    assert ledger.load(cfg.ledger_path) == []


# --- run_mp4_pipeline（FR-18: mp4 取込 → FR-17 の VTT 経路へ接続） ---------------

_MP4_SESSION = "recording-2026-06-01"


def _mp4_runner(repo: Path, *, duration: str = "600.0", convert_rc: int = 0, pipeline_rc: int = 0):
    """ffprobe / mp4→VTT / Unit B の3種の呼び出しを1つの seam で捌くフェイク。

    コマンド列で見分ける（ffprobe → mp4-to-vtt → postmeeting の順に呼ばれる）。
    """
    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[0] == "ffprobe":
            return SimpleNamespace(returncode=0, stdout=duration, stderr="")
        if any("mp4-to-vtt" in part or "mp4_to_vtt" in part for part in cmd):
            if convert_rc == 0:
                # 変換成功＝VTT が出来ている状態を作る（後段の VTT 経路が読む）。
                (repo / f"{_MP4_SESSION}.vtt").write_text("WEBVTT\n", encoding="utf-8")
            return SimpleNamespace(returncode=convert_rc, stdout="", stderr="convert boom")
        if pipeline_rc == 0:
            write_pipeline_outputs(repo, _MP4_SESSION, raw=False)  # mp4 経路の Transcribe は変換段で計上する
        return SimpleNamespace(returncode=pipeline_rc, stdout="", stderr="pipeline boom")

    return _run, calls


@pytest.mark.unit
def test_run_mp4_pipeline_records_transcribe_and_summary(cfg: MeetingConfig, repo: Path) -> None:
    mp4 = repo / f"{_MP4_SESSION}.mp4"
    mp4.write_bytes(b"\x00")
    run, _ = _mp4_runner(repo)

    lines: list[str] = []
    session_id, rc = pipeline.run_mp4_pipeline(cfg, mp4, claude=False, run=run, emit=lines.append)

    assert (session_id, rc) == (_MP4_SESSION, 0)
    # mp4 の Transcribe と、接続先 VTT 経路の要約段が同じ session に並ぶ。
    entries = ledger.load(cfg.ledger_path)
    assert {e.stage for e in entries} == {"transcribe", "bedrock"}
    assert {e.session for e in entries} == {_MP4_SESSION}


@pytest.mark.unit
def test_run_mp4_pipeline_transcribe_cost_uses_probed_duration(cfg: MeetingConfig, repo: Path) -> None:
    """mp4 は1系統のみ＝尺そのままが課金対象（paired の2系統合算とは異なる）。"""
    mp4 = repo / f"{_MP4_SESSION}.mp4"
    mp4.write_bytes(b"\x00")
    run, _ = _mp4_runner(repo, duration="600.0")

    pipeline.run_mp4_pipeline(cfg, mp4, claude=False, run=run, emit=lambda _: None)

    entry = next(e for e in ledger.load(cfg.ledger_path) if e.stage == "transcribe")
    assert entry.units == {"minutes": 10.0}


@pytest.mark.unit
def test_run_mp4_pipeline_aborts_before_billing_when_duration_unknown(cfg: MeetingConfig, repo: Path) -> None:
    """ffprobe が使えない＝見積が出せない場合、課金段（変換）へ入らず中止すること。"""
    mp4 = repo / f"{_MP4_SESSION}.mp4"
    mp4.write_bytes(b"\x00")

    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[0] == "ffprobe":
            raise FileNotFoundError("ffprobe")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    lines: list[str] = []
    session_id, rc = pipeline.run_mp4_pipeline(cfg, mp4, claude=False, run=run, emit=lines.append)

    assert (session_id, rc) == (_MP4_SESSION, 1)
    assert [c[0] for c in calls] == ["ffprobe"]  # 変換は起動しない。
    assert ledger.load(cfg.ledger_path) == []
    assert any("ffprobe" in line for line in lines)


@pytest.mark.unit
def test_run_mp4_pipeline_stops_on_convert_failure(cfg: MeetingConfig, repo: Path) -> None:
    mp4 = repo / f"{_MP4_SESSION}.mp4"
    mp4.write_bytes(b"\x00")
    run, calls = _mp4_runner(repo, convert_rc=1)

    lines: list[str] = []
    session_id, rc = pipeline.run_mp4_pipeline(cfg, mp4, claude=False, run=run, emit=lines.append)

    assert (session_id, rc) == (_MP4_SESSION, 1)
    assert any("convert boom" in line for line in lines)
    # 変換が失敗＝Transcribe が完走していないため台帳には積まない。
    assert ledger.load(cfg.ledger_path) == []
    assert not any("postmeeting" in " ".join(c) for c in calls)  # VTT 経路へは進まない。


@pytest.mark.unit
def test_run_mp4_pipeline_records_transcribe_even_if_summary_fails(cfg: MeetingConfig, repo: Path) -> None:
    """変換は完走＝Transcribe は課金済みなので、後段の失敗でも台帳へ残すこと。"""
    mp4 = repo / f"{_MP4_SESSION}.mp4"
    mp4.write_bytes(b"\x00")
    run, _ = _mp4_runner(repo, pipeline_rc=1)

    session_id, rc = pipeline.run_mp4_pipeline(cfg, mp4, claude=False, run=run, emit=lambda _: None)

    assert (session_id, rc) == (_MP4_SESSION, 1)
    assert {e.stage for e in ledger.load(cfg.ledger_path)} == {"transcribe"}


def _convert_call_count(calls: list[list[str]]) -> int:
    """mp4→VTT 変換ツール（課金段）の起動回数を数える。"""
    return sum(1 for c in calls if any("mp4-to-vtt" in part or "mp4_to_vtt" in part for part in c))


@pytest.mark.unit
def test_run_mp4_pipeline_reuses_existing_vtt_without_recharging(cfg: MeetingConfig, repo: Path) -> None:
    """2回目の mp4 取込は変換（課金段）を起動せず、既存 VTT から続ける。

    命名ゲートで停止した mp4 セッションの再開は「同じファイルを再指定」＝mp4 の再指定になる。
    変換をもう一度走らせると Transcribe を満額再課金するのに、台帳は has_entry で 1 行に
    留まる（N 回課金・最大 1 行）。変換自体を起動しないことで課金と記録の乖離を作らない。
    """
    mp4 = repo / f"{_MP4_SESSION}.mp4"
    mp4.write_bytes(b"\x00")
    run, calls = _mp4_runner(repo)

    pipeline.run_mp4_pipeline(cfg, mp4, claude=False, run=run, emit=lambda _: None)
    assert _convert_call_count(calls) == 1, "1回目は変換を起動すること"

    lines: list[str] = []
    session_id, rc = pipeline.run_mp4_pipeline(cfg, mp4, claude=False, run=run, emit=lines.append)

    assert (session_id, rc) == (_MP4_SESSION, 0)
    assert _convert_call_count(calls) == 1, "2回目は変換（課金段）を起動しないこと"
    assert any("再利用" in line for line in lines), "再利用したことが操作者に見えること"
    entries = ledger.load(cfg.ledger_path)
    assert sum(1 for e in entries if e.stage == "transcribe") == 1


@pytest.mark.unit
def test_run_mp4_pipeline_emits_estimate_before_billing(cfg: MeetingConfig, repo: Path) -> None:
    """見積の提示が課金段（mp4→VTT 変換）の起動より **前** であること。

    「見積の文字列が出ているか」だけを見ると、提示を課金の後ろへ移しても緑のままになる
    （操作者が金額を知る前に課金される）。emit と subprocess 起動を同じ列に記録して順序を固定する。
    """
    mp4 = repo / f"{_MP4_SESSION}.mp4"
    mp4.write_bytes(b"\x00")
    base_run, _ = _mp4_runner(repo)
    events: list[str] = []

    def run(cmd, **kwargs):
        if any("mp4-to-vtt" in part or "mp4_to_vtt" in part for part in cmd):
            events.append("convert")
        return base_run(cmd, **kwargs)

    lines: list[str] = []

    def emit(line: str) -> None:
        lines.append(line)
        if "今回見積" in line:
            events.append("estimate")

    pipeline.run_mp4_pipeline(cfg, mp4, claude=False, run=run, emit=emit)

    text = "\n".join(lines)
    assert "mp4 取込" in text
    assert "今月累計" in text
    assert events[:2] == ["estimate", "convert"], f"見積は課金段より前に出すこと: {events}"


@pytest.mark.unit
def test_record_summary_cost_zero_without_transcript(cfg: MeetingConfig) -> None:
    month = ledger.month_of(ledger.now_iso())
    assert pipeline.record_summary_cost(cfg, "meeting-x", False, month) == 0.0


@pytest.mark.unit
def test_record_summary_cost_dedups_on_rerun(cfg: MeetingConfig, repo: Path) -> None:
    write_pipeline_outputs(repo, "meeting-x", chars=500, raw=False)
    month = ledger.month_of(ledger.now_iso())

    first = pipeline.record_summary_cost(cfg, "meeting-x", False, month)
    assert first > 0

    second = pipeline.record_summary_cost(cfg, "meeting-x", False, month)
    assert second == 0.0
    assert sum(1 for e in ledger.load(cfg.ledger_path) if e.stage == "bedrock") == 1


@pytest.mark.unit
def test_record_summary_cost_recharges_when_minutes_regenerated_after_recording(
    cfg: MeetingConfig, repo: Path
) -> None:
    """meeting_info.json 更新（FR-MI-01）で要約段が再実行され minutes.md が再生成されたら、
    既に台帳へ記録済みの段でも新たな実行として再課金を記録する（has_entry の単純存在チェックでは
    2回目以降の実課金が幻の非課金になる）。
    """
    write_pipeline_outputs(repo, "meeting-x", chars=500, raw=False)
    month = ledger.month_of(ledger.now_iso())

    first = pipeline.record_summary_cost(cfg, "meeting-x", False, month)
    assert first > 0

    # 要約段の再実行（Bedrock 再課金）を模す: minutes.md を書き直し、mtime を明確に先へ進める。
    minutes_path = cfg.session_out_dir("meeting-x") / "minutes.md"
    minutes_path.write_text("# 議事録（再生成）\n## 決定事項\n- 承認\n", encoding="utf-8")
    future = minutes_path.stat().st_mtime + 10
    os.utime(minutes_path, (future, future))

    second = pipeline.record_summary_cost(cfg, "meeting-x", False, month)

    assert second > 0
    assert sum(1 for e in ledger.load(cfg.ledger_path) if e.stage == "bedrock") == 2


# --- next_steps_lines（純粋） --------------------------------------------------


@pytest.mark.unit
def test_next_steps_naming_required() -> None:
    lines = pipeline.next_steps_lines(Stage.NAMING_REQUIRED, _SESSION, False)
    assert any("speaker_names.json" in line for line in lines)
    assert any(f"meeting minutes {_SESSION}" in line for line in lines)


@pytest.mark.unit
def test_next_steps_naming_required_vtt_suggests_reimport() -> None:
    """VTTインポート由来（FR-17）は録音マニフェストが無く `meeting minutes` が使えないため、
    再実行の案内も VTTインポートボタンの再実行に変わること（実クラッシュの再発防止）。
    """
    lines = pipeline.next_steps_lines(Stage.NAMING_REQUIRED, _SESSION, False, vtt=True)
    assert any("VTTインポート" in line for line in lines)
    assert not any("meeting minutes" in line for line in lines)


@pytest.mark.unit
def test_next_steps_transcript_done_bedrock_vtt_suggests_reimport() -> None:
    lines = pipeline.next_steps_lines(Stage.TRANSCRIPT_DONE, _SESSION, False, vtt=True)
    assert any("VTTインポート" in line for line in lines)


@pytest.mark.unit
def test_next_steps_transcript_done_claude() -> None:
    lines = pipeline.next_steps_lines(Stage.TRANSCRIPT_DONE, _SESSION, True)
    assert any("SUMMARIZE_SKIPPED" in line for line in lines)


@pytest.mark.unit
def test_next_steps_transcript_done_bedrock() -> None:
    lines = pipeline.next_steps_lines(Stage.TRANSCRIPT_DONE, _SESSION, False)
    assert any("minutes.md が未生成" in line for line in lines)


@pytest.mark.unit
def test_next_steps_minutes_done() -> None:
    lines = pipeline.next_steps_lines(Stage.MINUTES_DONE, _SESSION, False)
    assert any("minutes.md" in line for line in lines)
    assert any("meeting slack" in line for line in lines)


@pytest.mark.unit
def test_next_steps_recorded_is_empty() -> None:
    assert pipeline.next_steps_lines(Stage.RECORDED, _SESSION, False) == []
