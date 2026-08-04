"""各段の状態検知と subprocess 起動（録音=Unit A / 議事録=Unit B のオーケストレーション）。

検知はファイル存在ベース（stdout パースより堅牢）。subprocess は seam（`runner` 引数）として
注入できるようにし、テストではモックする。課金・PII の実行は cli 側で「見積→実行→台帳」の
順に制御する。Slack 投稿はここには含めない（人間承認必須のため /subtext-slack に委ねる）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from datetime import datetime

from meeting.config import LIVE_EXE_NAME, RECORDER_EXE_NAME, MeetingConfig
from meeting.ledger import LedgerEntry, now_iso
from meeting.pricing import Estimate

# subprocess.run 互換の seam（テストで差し替え）。
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class Stage(str, Enum):
    """セッションが今どの段にあるか（ファイル存在から判定）。"""

    NO_RECORDING = "no_recording"  # manifest なし
    RECORDED = "recorded"  # 録音済・パイプライン未実行
    NAMING_REQUIRED = "naming_required"  # speaker_names.json 生成済・実名記入待ち
    TRANSCRIPT_DONE = "transcript_done"  # final_transcript.json まで完了（Claude 経路の停止点）
    MINUTES_DONE = "minutes_done"  # minutes.md 生成済


# 段の人間向けラベル。cli（status 表示）と wizard（現在段の提示）で共有する。
STAGE_LABELS: dict[Stage, str] = {
    Stage.NO_RECORDING: "未録音（manifest なし）",
    Stage.RECORDED: "録音済（議事録パイプライン未実行）",
    Stage.NAMING_REQUIRED: "話者名の記入待ち（speaker_names.json）",
    Stage.TRANSCRIPT_DONE: "トランスクリプト完了（議事録未生成）",
    Stage.MINUTES_DONE: "議事録生成済（minutes.md）",
}


@dataclass(frozen=True)
class StreamInfo:
    role: str  # self / others
    duration_sec: float


@dataclass(frozen=True)
class Manifest:
    session_id: str
    status: str  # complete / incomplete
    streams: tuple[StreamInfo, ...]

    @property
    def self_sec(self) -> float:
        return self._duration("self")

    @property
    def others_sec(self) -> float:
        return self._duration("others")

    def _duration(self, role: str) -> float:
        return sum(s.duration_sec for s in self.streams if s.role == role)


@dataclass(frozen=True)
class SessionSummary:
    """セッション一覧の1行（`meeting tui` の一覧表示向け）。

    録音(Unit A)由来は self_sec/others_sec が実測秒数を持つ。VTTインポート由来
    （FR-17・`--mode vtt`）は録音そのものが無いため None（status は固定文字列 "vtt"）。
    """

    session_id: str
    stage: Stage
    status: str  # manifest の録音状態（complete/incomplete）。VTTインポート由来は "vtt"
    self_sec: float | None
    others_sec: float | None


def list_sessions(cfg: MeetingConfig) -> list[SessionSummary]:
    """全セッションを新しい順（実体の最終更新時刻の降順）で返す。

    録音(Unit A)由来（data/recordings/<session>/manifest.json を持つ）と、VTTインポート由来
    （data/out/<session>/ のみに実体を持ち manifest が無い。FR-17）の両方を対象にする。
    """
    ordered = sorted(_all_session_ids(cfg), key=lambda s: _recency_key(cfg, s), reverse=True)
    return [_summarize_session(cfg, session_id) for session_id in ordered]


def _all_session_ids(cfg: MeetingConfig) -> set[str]:
    """セッションIDの母集団（録音由来＋取込由来）。順序は持たない。"""
    session_ids: set[str] = set()
    if cfg.recordings_dir.is_dir():
        session_ids.update(
            d.name for d in cfg.recordings_dir.iterdir() if d.is_dir() and (d / "manifest.json").is_file()
        )
    if cfg.out_dir.is_dir():
        session_ids.update(d.name for d in cfg.out_dir.iterdir() if d.is_dir())
    return session_ids


def _recency_key(cfg: MeetingConfig, session: str) -> tuple[float, str]:
    """「意味的に新しい」順序のキー（実体の最終更新時刻, セッションID）。

    セッションIDの名前空間が 2 系統混在する（録音=`yyyyMMdd-HHmmss` / 取込=ファイル名 stem）ため、
    **文字列比較では英字始まりの stem が必ずタイムスタンプに勝つ**。`live_captions`（ライブ字幕の
    既定出力名）や `Recording`（Teams 既定名）のディレクトリが一度できると、以降セッションを
    省略した全コマンドの既定がそれになり、`meeting slack` が別会議の minutes.md を投稿する
    （外部公開＝取り消せない・実名 PII の誤送信）。実体の最終更新時刻を正とし、同時刻は
    セッションIDで決定的に並べる。
    """
    dirs = (cfg.session_recording_dir(session), cfg.session_out_dir(session))
    mtimes = [d.stat().st_mtime for d in dirs if d.is_dir()]
    return (max(mtimes) if mtimes else 0.0, session)


def _summarize_session(cfg: MeetingConfig, session_id: str) -> SessionSummary:
    """セッション1件を一覧行へ写す（純粋な読み取り）。録音由来は実測秒、VTTインポート由来は "vtt"。"""
    status: str
    self_sec: float | None
    others_sec: float | None
    if has_manifest(cfg, session_id):
        manifest = read_manifest(cfg, session_id)
        status, self_sec, others_sec = manifest.status, manifest.self_sec, manifest.others_sec
    else:
        status, self_sec, others_sec = "vtt", None, None
    return SessionSummary(
        session_id=session_id,
        stage=detect_stage(cfg, session_id),
        status=status,
        self_sec=self_sec,
        others_sec=others_sec,
    )


def has_manifest(cfg: MeetingConfig, session: str) -> bool:
    """録音(Unit A)由来か（VTTインポート由来は manifest を持たないため False。FR-17）。"""
    return (cfg.session_recording_dir(session) / "manifest.json").is_file()


def resolve_session(cfg: MeetingConfig, session: str | None) -> str:
    """session 未指定なら全セッションの最新（実体の最終更新時刻）を返す。

    録音(Unit A)由来（recordings/<session>/manifest.json）と VTTインポート由来
    （data/out/<session>/ のみ・manifest なし。FR-17）の両方を対象にする（`list_sessions` と
    同じ母集団）。recordings のみを見ると、直近の作業が VTT インポートだった場合に古い録音
    セッションを既定に選んでしまい、`meeting slack` が別セッションの minutes.md を投稿し得る
    （録音専用の minutes/process は後段の has_manifest ガードで VTT 由来を弾く）。
    順序に名前を使わない理由は `_recency_key` を参照。
    """
    if session:
        return session.strip()
    candidates = _all_session_ids(cfg)
    if not candidates:
        raise FileNotFoundError(
            f"セッションが見つかりません（recordings={cfg.recordings_dir} / out={cfg.out_dir}）"
        )
    return max(candidates, key=lambda s: _recency_key(cfg, s))


def delete_session(cfg: MeetingConfig, session: str) -> None:
    """セッションのローカルファイルを削除する（録音ディレクトリ・議事録ディレクトリ）。

    コスト台帳（cost-ledger.jsonl）は追記専用の監査ログのため対象外（BR: 過去の課金記録は
    セッションファイルの有無に関わらず保持する）。存在しないディレクトリは無視する。
    """
    for d in (cfg.session_recording_dir(session), cfg.session_out_dir(session)):
        if d.is_dir():
            shutil.rmtree(d)


def read_manifest(cfg: MeetingConfig, session: str) -> Manifest:
    """recordings/<session>/manifest.json を読む（camelCase 契約に追従）。"""
    path = cfg.session_recording_dir(session) / "manifest.json"
    if not path.is_file():
        raise FileNotFoundError(f"manifest.json がありません: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    streams = tuple(
        StreamInfo(role=str(s["streamRole"]), duration_sec=float(s["durationSec"])) for s in data.get("streams", [])
    )
    return Manifest(
        session_id=str(data["sessionId"]),
        status=str(data["status"]),
        streams=streams,
    )


def detect_stage(cfg: MeetingConfig, session: str) -> Stage:
    """出力ファイルの存在から現在の段を判定する。

    録音(Unit A)由来（data/recordings/<session>/manifest.json）と VTTインポート由来
    （data/out/<session>/ のみに実体を持ち manifest は生成されない。FR-17）の両方に対応する。
    """
    out = cfg.session_out_dir(session)
    if not has_manifest(cfg, session) and not out.is_dir():
        return Stage.NO_RECORDING
    if (out / "minutes.md").is_file():
        return Stage.MINUTES_DONE
    if (out / "final_transcript.json").is_file():
        return Stage.TRANSCRIPT_DONE
    if (out / "speaker_names.json").is_file():
        return Stage.NAMING_REQUIRED
    return Stage.RECORDED


def transcript_char_count(cfg: MeetingConfig, session: str) -> int:
    """final_transcript.json のセグメント本文の総文字数（要約コスト概算の入力）。"""
    path = cfg.session_out_dir(session) / "final_transcript.json"
    if not path.is_file():
        raise FileNotFoundError(f"final_transcript.json がありません: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return sum(len(str(seg.get("text", ""))) for seg in data.get("segments", []))


def estimate_transcribe(cfg: MeetingConfig, session: str) -> Estimate:
    """manifest の録音秒数から Transcribe 概算を出す（paired=2系統合算）。"""
    manifest = read_manifest(cfg, session)
    return cfg.pricing.estimate_transcribe(manifest.self_sec, manifest.others_sec)


def estimate_summary(cfg: MeetingConfig, session: str, *, claude: bool) -> Estimate | None:
    """final_transcript.json があれば要約段の概算を返す。無ければ None（録音直後など）。"""
    out = cfg.session_out_dir(session) / "final_transcript.json"
    if not out.is_file():
        return None
    chars = transcript_char_count(cfg, session)
    return cfg.pricing.estimate_claude(chars) if claude else cfg.pricing.estimate_bedrock(chars)


def transcribe_ran(cfg: MeetingConfig, session: str) -> bool:
    """Transcribe 段が実行された痕跡（raw 出力）があるか＝課金が発生したか。

    台帳は「課金したら必ず記録する」ことが自動実行の前提のため、成否ではなく痕跡で判定する。
    一方で痕跡を見ずに無条件に計上すると、資格情報エラー等で課金前に落ちた実行にも幻の金額を
    積んでしまう。raw が無ければ Transcribe は走っておらず、計上すべき概算も無い。
    """
    out = cfg.session_out_dir(session)
    return any((out / f"raw_transcribe.{role}.json").is_file() for role in ("others", "self"))


def summarize_ran(cfg: MeetingConfig, session: str, *, claude: bool) -> bool:
    """要約段の課金が発生したか（Bedrock 経路は minutes.md、Claude 経路は最終成果物の到達で判定）。

    Bedrock 生成は minutes.md が要約段（Bedrock 呼び出し）の成果物そのもの。Claude 経路は
    Bedrock を踏まず、`final_transcript.json` が実名 PII を外部（Anthropic）へ渡す起点になるため、
    そこを計上点にする（pii_sent=True の記録が目的）。
    """
    out = cfg.session_out_dir(session)
    return (out / "final_transcript.json").is_file() if claude else (out / "minutes.md").is_file()


def correction_status(cfg: MeetingConfig, session: str) -> str | None:
    """final_transcript.json の `modelInfo.correction.status` を読む（無ければ None）。

    Unit B が書く補正段の監査メタ（FR-16 連携契約）。"skipped"（用語リスト空）は Bedrock を
    呼んでいない。それ以外（applied / dictionary_only / fallback）は LLM 呼び出しに入っている。
    """
    path = cfg.session_out_dir(session) / "final_transcript.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    correction = (data.get("modelInfo") or {}).get("correction") or {}
    status = correction.get("status")
    return str(status) if status is not None else None


def estimate_correction(cfg: MeetingConfig, session: str) -> Estimate | None:
    """C2 用語補正段（Bedrock）の概算。LLM を踏んだ痕跡が無ければ None。

    痕跡は `correction_status`。補正段は要約段と別の Bedrock 呼び出しで、既定経路では必ず走る。
    """
    status = correction_status(cfg, session)
    if status is None or status == "skipped":
        return None
    return cfg.pricing.estimate_correct(transcript_char_count(cfg, session))


# --- subprocess 起動（seam 経由） -------------------------------------------------


def resolve_recorder_exe(cfg: MeetingConfig) -> Path | None:
    """録音 exe を解決する。TargetFramework / ビルド構成は直書きしない。

    優先順: (1) self-contained publish 出力 dist/recorder（別PC移植の本命）→
    (2) dev ビルド出力 src/recorder/bin 配下を再帰探索し最新の exe。いずれも無ければ None。
    """
    if cfg.recorder_dist_exe.is_file():
        return cfg.recorder_dist_exe
    bin_dir = cfg.recorder_project_dir / "bin"
    if not bin_dir.is_dir():
        return None
    candidates = sorted(
        bin_dir.glob(f"**/{RECORDER_EXE_NAME}"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def needs_recorder_build(cfg: MeetingConfig) -> bool:
    """exe 未生成、または csproj が（dev ビルドの）exe より新しければビルドが要る。"""
    exe = resolve_recorder_exe(cfg)
    if exe is None:
        return True
    # publish 成果物（dist/）は明示生成物のため再ビルド判定の対象外。
    if exe == cfg.recorder_dist_exe:
        return False
    if not cfg.recorder_csproj.is_file():
        return False
    return cfg.recorder_csproj.stat().st_mtime > exe.stat().st_mtime


# 録音の既定最大分（appsettings 既定と揃える）。cli/tui 共通で参照する。
DEFAULT_RECORD_MINUTES = 180


def build_recorder_command(cfg: MeetingConfig, exe: Path, minutes: int) -> list[str]:
    """録音 exe の起動コマンド（最大録音分・停止ファイル・出力先を CLI 引数で渡す）。"""
    return [
        str(exe),
        "--minutes",
        str(minutes),
        "--stop-file",
        str(cfg.stop_file),
        "--out",
        str(cfg.recordings_dir),
    ]


# --- ライブ段（Unit C）の起動（B1F） ------------------------------------------


def new_session_id(now: datetime) -> str:
    """ライブ開始時刻からセッション ID（`yyyyMMdd-HHmmss`）を作る（録音の命名規約踏襲・純粋）。"""
    return now.strftime("%Y%m%d-%H%M%S")


def resolve_live_exe(cfg: MeetingConfig) -> Path | None:
    """ライブ字幕 exe を解決する（recorder と同方針。TargetFramework は直書きしない）。

    優先順: (1) self-contained publish 出力 dist/live → (2) dev ビルド出力
    src/live/bin 配下を再帰探索し最新の exe。いずれも無ければ None。
    """
    if cfg.live_dist_exe.is_file():
        return cfg.live_dist_exe
    bin_dir = cfg.live_project_dir / "bin"
    if not bin_dir.is_dir():
        return None
    candidates = sorted(
        bin_dir.glob(f"**/{LIVE_EXE_NAME}"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def needs_live_build(cfg: MeetingConfig) -> bool:
    """live exe 未生成、または csproj が（dev ビルドの）exe より新しければビルドが要る。"""
    exe = resolve_live_exe(cfg)
    if exe is None:
        return True
    if exe == cfg.live_dist_exe:  # publish 成果物は再ビルド判定の対象外。
        return False
    if not cfg.live_csproj.is_file():
        return False
    return cfg.live_csproj.stat().st_mtime > exe.stat().st_mtime


def build_live_env(cfg: MeetingConfig, session: str) -> dict[str, str]:
    """Unit C へ注入する環境変数（字幕 JSONL 出力先・停止ファイル・AWS プロファイル）。

    Unit C は CLI 引数でなく `SUBTEXT_Live__*` の設定上書きで受け取る（LiveSttConfig）。
    値のみでパス直書きはしない（[paths] 由来。BR-SEC-01: 秘密は含まない）。

    AWS プロファイルは Unit C が `.env` を読まないため、ここで渡さないとライブ字幕だけ
    別のプロファイル（SDK 既定）で動く。.NET SDK も `AWS_PROFILE` を見る。
    """
    return {
        "SUBTEXT_Live__CaptionSinkPath": str(cfg.live_captions_path(session)),
        "SUBTEXT_Live__StopFilePath": str(cfg.live_stop_file(session)),
        **aws_profile_env(cfg),
    }


def aws_profile_env(cfg: MeetingConfig) -> dict[str, str]:
    """子プロセスへ渡す AWS プロファイル指定（未指定なら空 dict）。

    未指定のときに `AWS_PROFILE=default` を入れてはならない（`~/.aws/config` に `[default]` が
    無い環境で `ProfileNotFound` になる）。詳細は Unit B の `aws.apply_profile` を参照。
    """
    return {"AWS_PROFILE": cfg.aws_profile} if cfg.aws_profile else {}


def run_live(
    cfg: MeetingConfig,
    exe: Path,
    session: str,
    *,
    runner: Runner = subprocess.run,
) -> "subprocess.CompletedProcess[str]":
    """Unit C ライブ字幕を前景起動する（字幕はコンソールへ。stdio は継承＝capture しない）。

    JSONL 出力先・停止ファイルは環境変数で注入する（FR-B1F-04）。実 AWS・実機実行境界の
    ため、字幕本文は本ハーネスでは扱わない（NFR-B1F-02）。
    """
    import os

    env = {**os.environ, **build_live_env(cfg, session)}
    return runner([str(exe)], cwd=str(cfg.repo_root), env=env)


def resolve_postmeeting_cmd() -> list[str]:
    """Unit B（subtext-postmeeting）を起動するコマンド接頭辞を解決する。

    recorder/live と違い dist 経路を持たない。TUI 自体が `uv run meeting` 起動である以上、
    配布先にも uv と workspace が要り、`uv sync` で Unit B も同じ .venv に入るため。
    """
    return ["uv", "run", "subtext-postmeeting"]


def build_pipeline_command(cfg: MeetingConfig, session: str, *, claude: bool) -> list[str]:
    """Unit B（subtext-postmeeting）の起動コマンド。--claude 時のみ --no-summarize。"""
    recording_dir = cfg.session_recording_dir(session)
    cmd = [
        *resolve_postmeeting_cmd(),
        "--mode",
        "paired",
        "--session",
        str(recording_dir),
    ]
    if claude:
        cmd.append("--no-summarize")  # Bedrock を呼ばず final_transcript.json で停止
    return cmd


def run_pipeline(
    cfg: MeetingConfig,
    session: str,
    *,
    claude: bool,
    runner: Runner = subprocess.run,
) -> "subprocess.CompletedProcess[str]":
    """Unit B（paired モード）をリポジトリルートを cwd にして起動する（課金境界・自動実行）。

    workspace 化により `uv run subtext-postmeeting` はルートから解決できるため cd 不要。
    出力先は OUTPUT_DIR=data/out を環境注入して段検知（out_dir）と一致させる。Unit B は
    .env を cwd から読むため、ルートの .env が拾われる（BR-SEC-01: 値はログに出さない）。
    """
    return _run_postmeeting(cfg, build_pipeline_command(cfg, session, claude=claude), runner=runner)


def vtt_session_id(vtt_path: Path) -> str:
    """VTTインポート（FR-17・`--mode vtt`）のセッションIDはファイル名(拡張子なし)由来。

    subtext-postmeeting 自身の規約（single モードと同方針）に追従する純粋関数。
    """
    return vtt_path.stem


def build_vtt_pipeline_command(cfg: MeetingConfig, vtt_path: Path, *, claude: bool) -> list[str]:
    """Unit B（subtext-postmeeting --mode vtt）の起動コマンド。--claude 時のみ --no-summarize。

    Transcribe/S3 を経由しない（FR-17）ため `--session` は無く、`--vtt` のみで完結する。
    起動接頭辞は paired と同じく resolve_postmeeting_cmd で解決する。
    """
    cmd = [*resolve_postmeeting_cmd(), "--mode", "vtt", "--vtt", str(vtt_path)]
    if claude:
        cmd.append("--no-summarize")
    return cmd


def launch_vtt_pipeline(
    cfg: MeetingConfig,
    vtt_path: Path,
    *,
    claude: bool,
    runner: Runner = subprocess.run,
) -> "subprocess.CompletedProcess[str]":
    """Unit B（vtt モード）をリポジトリルートを cwd にして起動する（run_pipeline と同方針）。"""
    return _run_postmeeting(cfg, build_vtt_pipeline_command(cfg, vtt_path, claude=claude), runner=runner)


# --- mp4 取込段（FR-18・subtext-mp4-to-vtt） ------------------------------------

# 尺取得に使う ffprobe（ffmpeg 同梱）。mp4→VTT 本体が要求する ffmpeg と同じ前提に乗る。
_FFPROBE = "ffprobe"


def resolve_mp4_to_vtt_cmd() -> list[str]:
    """mp4→VTT 独立ツール（FR-18）の起動コマンド接頭辞（resolve_postmeeting_cmd と同方針）。"""
    return ["uv", "run", "subtext-mp4-to-vtt"]


def mp4_vtt_output_path(mp4_path: Path) -> Path:
    """mp4 から生成する VTT の出力先（入力と同じ場所に `.vtt`）。

    `subtext-mp4-to-vtt` CLI の `--out` 既定と同一にし、CLI と TUI で出力先を分けない。
    セッションID（= ファイル名 stem）も後段の VTT 経路（vtt_session_id）と一致する。
    """
    return mp4_path.with_suffix(".vtt")


def probe_duration_sec(mp4_path: Path, *, runner: Runner = subprocess.run) -> float | None:
    """ffprobe で mp4 の尺（秒）を得る。取得できなければ None を返す。

    Transcribe は課金段のため、尺が分からない＝見積が出せない場合は呼び出し側で中止させる
    （台帳必須の方針上、金額不明のまま課金段へ入れない）。ffprobe 未導入・非対応ファイル・
    出力が数値でない、のいずれも None に畳む（原因の切り分けは呼び出し側の案内に委ねる）。
    """
    cmd = [
        _FFPROBE,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(mp4_path),
    ]
    try:
        proc = runner(cmd, capture_output=True, text=True, errors="replace")
    except OSError:
        return None  # ffprobe 未導入（FileNotFoundError）を含む。
    if proc.returncode != 0:
        return None
    try:
        duration = float((proc.stdout or "").strip())
    except ValueError:
        return None
    return duration if duration > 0 else None


def build_mp4_to_vtt_command(cfg: MeetingConfig, mp4_path: Path, out_vtt: Path) -> list[str]:
    """mp4→VTT（FR-18）の起動コマンド。出力先は明示して CLI 既定に依存させない。"""
    return [*resolve_mp4_to_vtt_cmd(), str(mp4_path), "--out", str(out_vtt)]


def launch_mp4_to_vtt(
    cfg: MeetingConfig,
    mp4_path: Path,
    out_vtt: Path,
    *,
    runner: Runner = subprocess.run,
) -> "subprocess.CompletedProcess[str]":
    """mp4→VTT をリポジトリルートを cwd にして起動する（.env をルートから拾わせるため）。

    OUTPUT_DIR はこのツールでは使われない（出力先は `--out`）が、cwd・出力キャプチャの方針を
    Unit B 起動と1箇所に揃えるため `_run_postmeeting` を共用する。
    """
    return _run_postmeeting(cfg, build_mp4_to_vtt_command(cfg, mp4_path, out_vtt), runner=runner)


def _run_postmeeting(
    cfg: MeetingConfig,
    cmd: list[str],
    *,
    runner: Runner,
    timeout_sec: float | None = None,
) -> "subprocess.CompletedProcess[str]":
    """subtext-postmeeting 共通の起動処理（cwd・OUTPUT_DIR 注入・出力キャプチャ）。

    `timeout_sec` を渡すと待ち上限を付ける（認証確認のような「返らなくても諦めたい」経路用。
    パイプライン本体は Transcribe のポーリングで長時間かかるため既定は無制限）。
    """
    import os

    env = {**os.environ, "OUTPUT_DIR": str(cfg.out_dir), **aws_profile_env(cfg)}
    return runner(
        cmd,
        cwd=str(cfg.repo_root),
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout_sec,
    )


# --- AWS 資格情報の疎通確認（FR-H2-11・BR-H2-AUTH-04） ---------------------------

# 確認の待ち上限（秒）。ネットワークが黒穴だと boto3 の既定リトライで分単位まで伸びるため、
# 表示のための確認が操作を待たせないようハーネス側で先に打ち切る。`uv run` のプロセス起動と
# boto3 の import ぶんの余裕を含める（打ち切っても `a` で再確認できる）。
CHECK_AUTH_TIMEOUT_SEC = 30.0
# Unit B が出す機械可読行のキー（`04-unit-b-会議後パイプライン.md` の契約）。
_AUTH_KEYS = ("authProfile", "authRegion", "authStatus")
# 対処メッセージとして拾う行数の上限（想定外の長い出力をログへ流し込まない）。
_AUTH_MESSAGE_MAX_LINES = 5


class AuthState(str, Enum):
    """資格情報の確認結果。`ok` 以外は AWS を使う段へ進めない状態を表す。"""

    OK = "ok"
    EXPIRED = "expired"  # 期限切れ・未設定 → 再ログインで解決する
    FORBIDDEN = "forbidden"  # 権限不足 → 再ログインでは解決しない
    PROFILE_NOT_FOUND = "profile_not_found"  # 指定プロファイルが無い → 綴り/設定の問題
    TRANSIENT = "transient"  # 一過性障害（リトライ後もなお失敗）
    UNKNOWN = "unknown"  # Unit B が分類できなかった
    ERROR = "error"  # 起動失敗・タイムアウト・出力が読めない（確認自体ができていない）


# 画面・ログ向けの短いラベル。
AUTH_STATE_LABELS: dict[AuthState, str] = {
    AuthState.OK: "OK",
    AuthState.EXPIRED: "認証切れ",
    AuthState.FORBIDDEN: "権限不足",
    AuthState.PROFILE_NOT_FOUND: "プロファイル不明",
    AuthState.TRANSIENT: "一時障害",
    AuthState.UNKNOWN: "不明",
    AuthState.ERROR: "確認失敗",
}


@dataclass(frozen=True)
class AuthStatus:
    """`--check-auth` の結果。profile / region は表示用（秘密ではない・BR-H2-AUTH-04）。"""

    state: AuthState
    profile: str = ""
    region: str = ""
    message: str = ""  # 対処メッセージ（OK なら空）

    @property
    def ok(self) -> bool:
        return self.state is AuthState.OK

    @property
    def label(self) -> str:
        return AUTH_STATE_LABELS[self.state]


def build_check_auth_command() -> list[str]:
    """Unit B の資格情報確認（課金なし）の起動コマンド。"""
    return [*resolve_postmeeting_cmd(), "--check-auth"]


def parse_auth_output(stdout: str, *, stderr: str = "") -> AuthStatus:
    """`--check-auth` の stdout を AuthStatus へ畳む（純粋）。

    `authStatus=` が無い出力は「確認できなかった」として ERROR に倒す（成功と区別する）。
    その場合の原因は Unit B の出力にしか無いため、先頭数行を対処メッセージとして拾う。
    """
    values: dict[str, str] = {}
    rest: list[str] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        key, sep, value = line.partition("=")
        if sep and key in _AUTH_KEYS:
            values[key] = value.strip()
        else:
            rest.append(raw.rstrip())  # 対処メッセージは字下げごと残す（読みやすさのため）

    message = "\n".join(rest[:_AUTH_MESSAGE_MAX_LINES])
    raw_state = values.get("authStatus", "")
    if not raw_state:
        # stderr は**末尾**を採る。`uv run` は解決/インストールの進捗を stderr へ先に書くため、
        # 先頭から採ると原因（最後に出る Unit B のエラー行）が落ちて進捗表示だけが残る。
        detail = message or "\n".join(stderr.strip().splitlines()[-_AUTH_MESSAGE_MAX_LINES:])
        return AuthStatus(
            AuthState.ERROR,
            message=detail or "AWS 認証の確認結果を読み取れませんでした（uv run subtext-postmeeting --check-auth）。",
        )
    try:
        state = AuthState(raw_state)
    except ValueError:
        # Unit B 側に新しい分類が増えた場合。成功と誤認しないよう UNKNOWN 扱いにする。
        state = AuthState.UNKNOWN
    return AuthStatus(
        state,
        profile=values.get("authProfile", ""),
        region=values.get("authRegion", ""),
        message="" if state is AuthState.OK else message,
    )


def run_check_auth(cfg: MeetingConfig, *, runner: Runner = subprocess.run) -> AuthStatus:
    """Unit B に資格情報を確認させる（課金なし・FR-H2-11）。

    ハーネスは boto3 を持たず、Unit B の `auth_policy` も import できない（FR-16）。
    そのためプロセス起動と stdout の契約だけで結果を受け取る。確認できなかったこと自体も
    状態として返し、例外で呼び出し側を落とさない（起動時に必ず走る経路のため）。
    """
    try:
        proc = _run_postmeeting(
            cfg, build_check_auth_command(), runner=runner, timeout_sec=CHECK_AUTH_TIMEOUT_SEC
        )
    except subprocess.TimeoutExpired:
        return AuthStatus(
            AuthState.ERROR,
            message=f"AWS 認証の確認が {CHECK_AUTH_TIMEOUT_SEC:.0f} 秒で応答しませんでした（ネットワークを確認してください）。",
        )
    except OSError as exc:
        # uv が PATH に無い等。ここで落とすと TUI が起動できなくなる。
        return AuthStatus(AuthState.ERROR, message=f"AWS 認証の確認を起動できませんでした: {exc}")
    return parse_auth_output(proc.stdout or "", stderr=proc.stderr or "")


def build_ledger_entry(
    session: str,
    estimate: Estimate,
    *,
    unit_price_usd: float,
    cumulative_month_usd: float,
    units: dict[str, float],
    ts: str | None = None,
    profile: str = "",
) -> LedgerEntry:
    """概算結果を台帳1行に変換する。backend は段から導出（claude のみ piiSent）。

    `profile` は実行時の AWS プロファイル（空＝未指定）。どの口座に出た支出かを残す。
    """
    backend = "claude" if estimate.stage == "claude" else "aws"
    return LedgerEntry(
        ts=ts or now_iso(),
        session=session,
        stage=estimate.stage,
        backend=backend,
        est_usd=estimate.usd,
        unit_price_usd=unit_price_usd,
        cumulative_month_usd=cumulative_month_usd,
        pii_sent=estimate.pii_sent,
        units=units,
        profile=profile,
    )
