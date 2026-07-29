"""runner/config テスト用の擬似リポジトリ構築 fixture。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from meeting.config import MeetingConfig, load_config

# 実 meeting.toml（単価・閾値の正本）をテストでも使う。
_MEETING_TOML = Path(__file__).resolve().parent.parent / "meeting.toml"


def write_manifest(repo: Path, session: str, *, self_sec: float, others_sec: float, status: str = "complete") -> None:
    """data/recordings/<session>/manifest.json を camelCase 契約で書く。"""
    rec_dir = repo / "data" / "recordings" / session
    rec_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "sessionId": session,
        "createdAtUtc": "2026-06-25T12:01:56+00:00",
        "status": status,
        "commonStartUtc": "2026-06-25T12:01:56+00:00",
        "streams": [
            {"streamRole": "self", "durationSec": self_sec},
            {"streamRole": "others", "durationSec": others_sec},
        ],
    }
    (rec_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


def write_out_file(repo: Path, session: str, name: str, content: str = "{}") -> Path:
    """data/out/<session>/<name> を作る。"""
    out_dir = repo / "data" / "out" / session
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    path.write_text(content, encoding="utf-8")
    return path


def write_pipeline_outputs(
    repo: Path,
    session: str,
    *,
    chars: int = 1000,
    raw: bool = True,
    minutes: bool = True,
    correction_status: str | None = None,
) -> None:
    """Unit B が残す成果物一式を模して書く（段の痕跡＝コスト計上の根拠）。

    ハーネスは「その段の成果物がある＝段が実行された＝課金された」で台帳を突き合わせる
    （pipeline.record_costs）。実 Unit B と同じ痕跡を置かないと、実際には課金が起きているのに
    計上されない（またはその逆の）テストと実装の食い違いを作ってしまう。

    - `raw`: Transcribe 段の痕跡（paired 経路のみ。VTT/mp4 取込は Transcribe を経ない）
    - `minutes`: 要約段（Bedrock）の痕跡。Claude 経路は minutes.md を作らないため False
    - `correction_status`: C2 用語補正段の監査メタ（"applied" 等。None なら補正なし＝計上対象外）
    """
    if raw:
        write_out_file(repo, session, "raw_transcribe.others.json", "{}")
        write_out_file(repo, session, "raw_transcribe.self.json", "{}")
    final: dict[str, Any] = {"segments": [{"text": "x" * chars}]}
    if correction_status is not None:
        final["modelInfo"] = {"correction": {"status": correction_status}}
    write_out_file(repo, session, "final_transcript.json", json.dumps(final, ensure_ascii=False))
    if minutes:
        write_out_file(repo, session, "minutes.md", "# 議事録\n## 決定事項\n- 承認\n")


@pytest.fixture(autouse=True)
def _isolate_slack_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """実環境の SLACK_BOT_TOKEN を全テストから取り除く（BR-SEC-01 の検証を空振りさせないため）。

    `slack.resolve_slack_token` は環境変数をルート .env より優先して返す。開発機や CI に実トークンが
    export されていると、テストが用意した .env のトークンではなく実トークンが使われ、
    「トークンがログに出ていないか」という assert が別の値を検証してしまう（＝漏洩を検出できない）。
    実トークンが pytest 出力へ出る恐れもあるため、個々のテストの delenv に頼らず一律で外す。
    """
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Subtext.sln マーカと data/ 配下を持つ最小リポジトリ。"""
    (tmp_path / "Subtext.sln").write_text("", encoding="utf-8")
    (tmp_path / "src" / "postmeeting").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "recordings").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def cfg(repo: Path) -> MeetingConfig:
    """擬似リポジトリを指す MeetingConfig（台帳は repo 内に隔離）。"""
    return load_config(
        meeting_toml=_MEETING_TOML,
        start_dir=repo,
        env={"SUBTEXT_MEETING_LEDGER": str(repo / "cost-ledger.jsonl")},
    )
