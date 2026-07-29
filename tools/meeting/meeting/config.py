"""会議ハーネスの設定読込（meeting.toml + 環境変数）とパス解決。

リポジトリ構成（疎結合, FR-16）に追従するだけで、ユニット契約には踏み込まない:
  data/recordings/<session>/manifest.json  … Unit A 出力（見積入力）
  data/out/<session>/...                    … Unit B 出力（段検知の対象）
  tools/meeting/cost-ledger.jsonl           … 本ハーネスのコスト台帳

単価・閾値・**レイアウト（[paths]）** はすべて meeting.toml が正。コードにパスや
TargetFramework を直書きしない（別PC・別構成へ移植できるようにするため）。各パスは
リポジトリルート（Subtext.sln のある階層）を find_repo_root で解決してから相対結合する。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from meeting.ledger import Thresholds
from meeting.pricing import Pricing

# リポジトリルートの目印（このファイルが在るディレクトリを上位探索する）。
_REPO_MARKER = "Subtext.sln"
# 録音 exe / csproj のファイル名（配置は [paths] とビルド出力探索で解決する）。
RECORDER_EXE_NAME = "Subtext.Recorder.exe"
RECORDER_CSPROJ_NAME = "Subtext.Recorder.csproj"
# ライブ字幕 exe / csproj（B1F・live 段。録音と同じく [paths] とビルド出力探索で解決）。
LIVE_EXE_NAME = "Subtext.Live.exe"
LIVE_CSPROJ_NAME = "Subtext.Live.csproj"
# [paths] 各キーの既定値（meeting.toml に無い場合のフォールバック。全てルート相対）。
_DEFAULT_PATHS = {
    "recordings_dir": "data/recordings",
    "out_dir": "data/out",
    "postmeeting_dir": "src/postmeeting",
    "recorder_project": "src/recorder",
    "recorder_dist": "dist/recorder",
    "live_project": "src/live",
    "live_dist": "dist/live",
}


@dataclass(frozen=True)
class MeetingConfig:
    """解決済みのパス群と単価・閾値。runner/cli はこれだけを参照する。"""

    repo_root: Path
    recordings_dir: Path
    postmeeting_dir: Path
    out_dir: Path  # data/out（この配下に <session>/ が並ぶ）
    ledger_path: Path
    stop_file: Path
    recorder_project_dir: Path  # src/recorder（csproj / bin の親）
    recorder_dist_dir: Path  # dist/recorder（self-contained publish 出力）
    live_project_dir: Path  # src/live（csproj / bin の親。B1F live 段）
    live_dist_dir: Path  # dist/live（self-contained publish 出力。B1F live 段）
    pricing: Pricing
    thresholds: Thresholds
    slack_default_channel: str  # 議事録の既定投稿先チャンネルID（非秘密・空可）。Token は .env

    @property
    def recorder_csproj(self) -> Path:
        return self.recorder_project_dir / RECORDER_CSPROJ_NAME

    @property
    def recorder_dist_exe(self) -> Path:
        return self.recorder_dist_dir / RECORDER_EXE_NAME

    @property
    def live_csproj(self) -> Path:
        return self.live_project_dir / LIVE_CSPROJ_NAME

    @property
    def live_dist_exe(self) -> Path:
        return self.live_dist_dir / LIVE_EXE_NAME

    def session_out_dir(self, session: str) -> Path:
        return self.out_dir / session

    def live_captions_path(self, session: str) -> Path:
        """ライブ字幕 JSONL の出力先（B1F・FR-B1F-01）。"""
        return self.session_out_dir(session) / "live_captions.jsonl"

    def live_stop_file(self, session: str) -> Path:
        """ライブ段の停止シグナル（B1F・FR-B1F-03。録音の stop_file とは別・セッション配下）。"""
        return self.session_out_dir(session) / ".stop"

    def session_recording_dir(self, session: str) -> Path:
        return self.recordings_dir / session


def find_repo_root(start: Path) -> Path:
    """`Subtext.sln` を持つ最も近い上位ディレクトリを返す。見つからなければ例外。"""
    for candidate in (start, *start.parents):
        if (candidate / _REPO_MARKER).is_file():
            return candidate
    raise FileNotFoundError(f"リポジトリルート（{_REPO_MARKER} を含む親）が {start} から見つかりません")


def _default_meeting_toml() -> Path:
    """meeting.toml は本パッケージの1つ上（tools/meeting/）に置く規約。"""
    return Path(__file__).resolve().parent.parent / "meeting.toml"


def load_config(
    *,
    meeting_toml: Path | None = None,
    start_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> MeetingConfig:
    """meeting.toml と環境変数から設定を構築する。

    引数は全てテスト用の seam（既定は実ファイル・cwd・os.environ）。
    """
    import os

    env = os.environ if env is None else env
    start = (start_dir or Path.cwd()).resolve()
    toml_path = meeting_toml or _default_meeting_toml()

    repo_root = find_repo_root(start)
    with toml_path.open("rb") as fh:
        cfg = tomllib.load(fh)

    pricing = Pricing.from_config(cfg)
    thresholds = Thresholds.from_config(cfg)

    # [paths] を単一の正としてレイアウトを解決する（未指定キーは _DEFAULT_PATHS へフォールバック）。
    paths = cfg.get("paths", {})

    def resolve(key: str) -> Path:
        raw = str(paths.get(key, _DEFAULT_PATHS[key])).strip() or _DEFAULT_PATHS[key]
        return repo_root / raw

    recordings_dir = resolve("recordings_dir")
    out_dir = resolve("out_dir")

    # [slack]（任意）。既定投稿先チャンネル（非秘密）のみ。Bot Token は .env（BR-SEC-01）。
    slack_default_channel = str(cfg.get("slack", {}).get("default_channel", "")).strip()

    # 台帳は環境変数で差し替え可（既定は tools/meeting/cost-ledger.jsonl）。
    ledger_override = env.get("SUBTEXT_MEETING_LEDGER")
    ledger_path = Path(ledger_override) if ledger_override else toml_path.parent / "cost-ledger.jsonl"

    return MeetingConfig(
        repo_root=repo_root,
        recordings_dir=recordings_dir,
        postmeeting_dir=resolve("postmeeting_dir"),
        out_dir=out_dir,
        ledger_path=ledger_path,
        stop_file=recordings_dir / ".stop",
        recorder_project_dir=resolve("recorder_project"),
        recorder_dist_dir=resolve("recorder_dist"),
        live_project_dir=resolve("live_project"),
        live_dist_dir=resolve("live_dist"),
        pricing=pricing,
        thresholds=thresholds,
        slack_default_channel=slack_default_channel,
    )
