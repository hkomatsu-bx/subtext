"""後処理ウィザード（meeting process）のテスト。

パイプライン実行（run_minutes）と対話入力（ask）は seam でモックし、課金・実 subprocess を
起こさない。段遷移はファイル生成を模した副作用で再現する（detect_stage はファイル存在で判定）。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from meeting import ledger, wizard
from meeting.cli import main
from meeting.config import MeetingConfig

# conftest を直接参照する（詳細は test_runner.py の同 import の注記を参照）。
from conftest import write_manifest, write_out_file, write_pipeline_outputs

_SESSION = "20260625-120156"


def _naming_json(session: str, *, mappings: dict, clusters: list) -> str:
    return json.dumps(
        {
            "sessionId": session,
            "mappings": mappings,
            "unresolved": [],
            "_clusters": clusters,
        },
        ensure_ascii=False,
    )


def _read_names(repo: Path, session: str) -> dict:
    path = repo / "data" / "out" / session / "speaker_names.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.integration
def test_wizard_bedrock_recorded_to_minutes(cfg: MeetingConfig, repo: Path, capsys) -> None:
    write_manifest(repo, _SESSION, self_sec=120.0, others_sec=120.0)

    # run_minutes を2回に分けて段を進める: 1回目=話者名ゲート生成、2回目=minutes 生成。
    steps = iter(
        [
            lambda: write_out_file(
                repo,
                _SESSION,
                "speaker_names.json",
                _naming_json(
                    _SESSION,
                    mappings={"spk_0": "", "self": "自分"},
                    clusters=[
                        {
                            "label": "spk_0",
                            "sampleUtterances": ["おはようございます"],
                            "segmentCount": 4,
                            "totalSec": 20.0,
                        }
                    ],
                ),
            ),
            lambda: (
                write_out_file(repo, _SESSION, "final_transcript.json", "{}"),
                write_out_file(repo, _SESSION, "minutes.md", "# 議事録\n## 決定事項\n- 承認"),
            ),
        ]
    )

    def run_minutes(_session: str) -> int:
        next(steps)()
        return 0

    answers = iter(["山田太郎", "", "", ""])
    rc = wizard.run(cfg, _SESSION, claude=False, run_minutes=run_minutes, ask=lambda _prompt: next(answers))

    out = capsys.readouterr().out
    assert rc == 0
    assert "決定事項" in out  # minutes.md が表示される
    names = _read_names(repo, _SESSION)
    assert names["mappings"]["spk_0"] == "山田太郎"
    assert names["mappings"]["self"] == "自分"


@pytest.mark.integration
def test_wizard_claude_stops_at_transcript(cfg: MeetingConfig, repo: Path, capsys) -> None:
    write_manifest(repo, _SESSION, self_sec=120.0, others_sec=120.0)

    steps = iter(
        [
            lambda: write_out_file(
                repo,
                _SESSION,
                "speaker_names.json",
                _naming_json(
                    _SESSION,
                    mappings={"spk_0": "", "self": "自分"},
                    clusters=[{"label": "spk_0", "sampleUtterances": [], "segmentCount": 1, "totalSec": 5.0}],
                ),
            ),
            # Claude 経路: final_transcript.json まで（minutes.md は作らない）。
            lambda: write_out_file(repo, _SESSION, "final_transcript.json", "{}"),
        ]
    )

    def run_minutes(_session: str) -> int:
        next(steps)()
        return 0

    rc = wizard.run(cfg, _SESSION, claude=True, run_minutes=run_minutes, ask=lambda _prompt: "")

    out = capsys.readouterr().out
    assert rc == 0
    assert "Claude 生成経路" in out
    assert "== 議事録: data/out" not in out  # minutes 表示はされない


@pytest.mark.integration
def test_wizard_aborts_when_no_progress(cfg: MeetingConfig, repo: Path, capsys) -> None:
    write_manifest(repo, _SESSION, self_sec=120.0, others_sec=120.0)

    def run_minutes(_session: str) -> int:
        return 0  # ファイルを生成しない＝段が進まない

    rc = wizard.run(cfg, _SESSION, claude=False, run_minutes=run_minutes, ask=lambda _prompt: "")

    out = capsys.readouterr().out
    assert rc == 1
    assert "進捗しませんでした" in out


@pytest.mark.integration
def test_wizard_fills_only_empty_labels(cfg: MeetingConfig, repo: Path) -> None:
    write_manifest(repo, _SESSION, self_sec=60.0, others_sec=60.0)
    # 開始時点で NAMING_REQUIRED（speaker_names.json あり・final/minutes なし）。
    write_out_file(
        repo,
        _SESSION,
        "speaker_names.json",
        _naming_json(
            _SESSION,
            mappings={"spk_0": "既存氏名", "spk_1": "", "self": "自分"},
            clusters=[
                {"label": "spk_0", "sampleUtterances": ["A"], "segmentCount": 2, "totalSec": 8.0},
                {"label": "spk_1", "sampleUtterances": ["B"], "segmentCount": 3, "totalSec": 9.0},
            ],
        ),
    )

    def run_minutes(_session: str) -> int:
        write_out_file(repo, _SESSION, "final_transcript.json", "{}")
        write_out_file(repo, _SESSION, "minutes.md", "# m")
        return 0

    asked: list[str] = []

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return "佐藤花子"

    rc = wizard.run(cfg, _SESSION, claude=False, run_minutes=run_minutes, ask=ask)

    assert rc == 0
    names = _read_names(repo, _SESSION)
    assert names["mappings"]["spk_0"] == "既存氏名"  # 既記入は温存
    assert names["mappings"]["spk_1"] == "佐藤花子"  # 空欄のみ記入
    assert names["mappings"]["self"] == "自分"  # self は温存
    assert sum(1 for p in asked if "の実名" in p) == 1  # 空欄ラベルのみ質問


@pytest.mark.integration
def test_process_end_to_end_records_costs(cfg: MeetingConfig, repo: Path, capsys) -> None:
    """main(["process", ...]) の結合: run/ask を seam で注入し台帳記録まで確認する。"""
    write_manifest(repo, _SESSION, self_sec=120.0, others_sec=120.0)

    state = {"n": 0}

    def run(cmd, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            write_out_file(
                repo,
                _SESSION,
                "speaker_names.json",
                _naming_json(
                    _SESSION,
                    mappings={"spk_0": "", "self": "自分"},
                    clusters=[{"label": "spk_0", "sampleUtterances": ["やあ"], "segmentCount": 2, "totalSec": 6.0}],
                ),
            )
        else:
            write_pipeline_outputs(repo, _SESSION)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    rc = main(["process", _SESSION], cfg=cfg, run=run, ask=lambda _prompt: "田中一郎")

    out = capsys.readouterr().out
    assert rc == 0
    assert "決定事項" in out
    assert _read_names(repo, _SESSION)["mappings"]["spk_0"] == "田中一郎"

    stages = {e.stage for e in ledger.load(cfg.ledger_path)}
    assert "transcribe" in stages
    assert "bedrock" in stages


@pytest.mark.integration
def test_process_no_recording_errors(cfg: MeetingConfig) -> None:
    rc = main(["process", "nope"], cfg=cfg, run=lambda *a, **k: None, ask=lambda _prompt: "")
    assert rc == 1


@pytest.mark.integration
def test_wizard_shows_sample_utterance_and_count(cfg: MeetingConfig, repo: Path, capsys) -> None:
    """話者名ゲートが Unit B の camelCase クラスタから発話数・代表発言を提示する。"""
    write_manifest(repo, _SESSION, self_sec=60.0, others_sec=60.0)
    write_out_file(
        repo,
        _SESSION,
        "speaker_names.json",
        _naming_json(
            _SESSION,
            mappings={"spk_0": "", "self": "自分"},
            clusters=[
                {"label": "spk_0", "sampleUtterances": ["これは代表発言です"], "segmentCount": 7, "totalSec": 30.0}
            ],
        ),
    )

    def run_minutes(_session: str) -> int:
        write_out_file(repo, _SESSION, "final_transcript.json", "{}")
        write_out_file(repo, _SESSION, "minutes.md", "# m")
        return 0

    rc = wizard.run(cfg, _SESSION, claude=False, run_minutes=run_minutes, ask=lambda _prompt: "山田太郎")

    out = capsys.readouterr().out
    assert rc == 0
    assert "発話数 7" in out
    assert "これは代表発言です" in out


@pytest.mark.integration
def test_wizard_survives_null_naming_fields(cfg: MeetingConfig, repo: Path) -> None:
    """speaker_names.json のキーが JSON null でも TypeError で落ちない。"""
    write_manifest(repo, _SESSION, self_sec=60.0, others_sec=60.0)
    write_out_file(
        repo, _SESSION, "speaker_names.json", json.dumps({"sessionId": _SESSION, "mappings": None, "_clusters": None})
    )

    def run_minutes(_session: str) -> int:
        write_out_file(repo, _SESSION, "final_transcript.json", "{}")
        write_out_file(repo, _SESSION, "minutes.md", "# m")
        return 0

    rc = wizard.run(cfg, _SESSION, claude=False, run_minutes=run_minutes, ask=lambda _prompt: "")
    assert rc == 0
