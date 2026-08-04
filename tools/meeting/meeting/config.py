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
# AWS プロファイルを選ぶ環境変数（SDK 共通。Unit B の boto3・Unit C の .NET SDK が同じ変数を見る）。
_PROFILE_ENV = "AWS_PROFILE"
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
    # 子プロセス（Unit B・mp4→VTT・Unit C）へ渡す AWS プロファイル名。空＝未指定（SDK 既定）。
    # `--profile` 指定時は cli が dataclasses.replace で差し替える。
    aws_profile: str = ""

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

    # AWS プロファイル: 環境変数 → ルート .env（`--profile` は cli が上書きする）。
    # ハーネス自身が .env を読むのは Unit C のためである（Unit C は .env を読まないので、
    # 注入しないとライブ字幕だけ別プロファイルで動く）。
    aws_profile = (env.get(_PROFILE_ENV) or read_env_value(repo_root / ".env", _PROFILE_ENV) or "").strip()

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
        aws_profile=aws_profile,
    )


def read_env_value(path: Path, key: str) -> str | None:
    """ルート `.env` から 1 キーだけ取り出す最小パーサ（KEY=VALUE のみ・純粋）。

    `#` コメント行・空行・`=` を含まない行は無視する。`export KEY=...` 形式を許容し、
    値は両端クオート除去とインラインコメント（非クオート値の ` #` 以降）除去を行う。
    `.env` の変数展開等は実装しない（YAGNI）。読取失敗（OSError）は None を返す。

    利用者は 2 つある（Slack Bot Token と AWS プロファイル）。値の扱いは呼び出し側の責務で、
    秘密（Token）はログに出さない（BR-SEC-01）。
    """
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        name = name.strip()
        if name.startswith("export "):
            name = name[len("export ") :].strip()
        if name == key:
            return _parse_env_value(value)
    return None


def _parse_env_value(raw: str) -> str | None:
    """`.env` の値部分を解釈する（両端クオート除去・インラインコメント除去・純粋）。

    クオート囲みなら閉じクオートまでを値とし以降（コメント含む）を無視する。非クオートなら
    ` #` 以降をインラインコメントとして落とす（トークンやプロファイル名に空白/`#` は含まれない前提）。
    """
    value = raw.strip()
    if not value:
        return None
    if value[0] in ('"', "'"):
        quote = value[0]
        end = value.find(quote, 1)
        if end != -1:
            return value[1:end] or None
        value = value[1:]  # 閉じクオート無し: 開きだけ外して継続
    else:
        comment = value.find(" #")
        if comment != -1:
            value = value[:comment]
    return value.strip() or None
