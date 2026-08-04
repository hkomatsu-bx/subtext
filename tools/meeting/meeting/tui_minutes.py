"""議事録フロー（取込 → 命名ゲート → 議事録）の進行管理。

`tui.py` から切り出した coordinator。TUI 本体が持っていた「経路の記憶」と「自動再開の回数」は
画面の状態ではなくこのフローの状態なので、ここへ集める。

- 経路（`MinutesRoute`）: 録音由来は `--mode paired`（引数は manifest から導出できる）、
  取込由来は `--mode vtt` で元の VTT パスが必要になる。後者は TUI が保持するしかないため
  `_vtt_sources` に置く（プロセス内のみ・永続化しない。再起動後は取込からやり直す）。
- 命名ゲート（BR-NAME-01）: 停止したらモーダルを開き、記入して続行すればそのまま再開する。
  実名と代表発言（PII）はモーダル内にのみ表示し、ログへは件数だけ書く（BR-NAME-04）。

課金・台帳追記という外部副作用があるため、実行中の同種ワーカーはキャンセルではなく拒否する
（キャンセルしても thread ワーカーの子プロセスは止まらず、課金だけが進む）。
画面操作（ログ出力・モーダル表示・ワーカー起動）は TUI 本体へ委譲する。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from meeting import naming, pipeline, runner
from meeting.runner import SessionSummary, Stage
from meeting.tui_modals import SpeakerNamesModal
from meeting.tui_view import Activity

if TYPE_CHECKING:  # 実行時 import は循環参照になるため型検査時のみ。
    from meeting.tui import MeetingApp

# ワーカーグループ（同種操作の二重起動を弾く単位）。
MINUTES_WORKER_GROUP = "minutes"
VTT_WORKER_GROUP = "vtt"
MP4_WORKER_GROUP = "mp4"
# 取込で mp4 取込段（FR-18）へ回す拡張子。他は VTT 直接取込（FR-17）として扱う。
_MP4_SUFFIX = ".mp4"
# 命名ゲートからの自動再開の上限（1セッションあたり）。ゲートは設計上1回しか現れないため、
# 進捗しない異常時にモーダル→実行を延々と繰り返さないための保険。
_MAX_AUTO_RESUME = 1
# ワーカー完了ハンドラで経路(Bedrock/Claude)を判別するための worker name（共有属性の競合を
# 避けるため。minutes/vtt の各ワーカーが並行実行され得る＝グループが異なる）。
CLAUDE_WORKER_NAME = "claude"
BEDROCK_WORKER_NAME = "bedrock"


def worker_name(claude: bool) -> str:
    """ワーカー名（完了ハンドラが経路を判別するために使う）。"""
    return CLAUDE_WORKER_NAME if claude else BEDROCK_WORKER_NAME


@dataclass(frozen=True)
class MinutesRoute:
    """議事録パイプラインの実行/再開手段。

    `vtt_path` が None なら録音由来（`--mode paired`）、非 None なら VTT/mp4 由来（`--mode vtt`）。
    """

    session_id: str
    vtt_path: Path | None

    @property
    def worker_group(self) -> str:
        return MINUTES_WORKER_GROUP if self.vtt_path is None else VTT_WORKER_GROUP


class MinutesFlow:
    """取込・命名ゲート・議事録生成の進行を持つ（画面操作は TUI 本体へ委譲）。"""

    def __init__(self, app: "MeetingApp") -> None:
        self._app = app
        # 取込由来セッションの入力 VTT パス（命名ゲートからの再開に必要）。
        self._vtt_sources: dict[str, Path] = {}
        # 命名ゲートからの自動再開回数（セッション別。_MAX_AUTO_RESUME で打ち切る）。
        self._auto_resume_counts: dict[str, int] = {}

    # --- 経路の記憶 ------------------------------------------------------------

    def forget(self, session_id: str) -> None:
        """セッション削除時に記憶を捨てる（同一IDの再取込で前回の状態が混ざらないように）。"""
        self._vtt_sources.pop(session_id, None)
        self._auto_resume_counts.pop(session_id, None)

    def resolve_route(self, session_id: str) -> MinutesRoute | None:
        """セッションの実行経路を決める。VTT 由来で入力パスが不明なら None（案内へ回す）。"""
        if runner.has_manifest(self._app.cfg, session_id):
            return MinutesRoute(session_id=session_id, vtt_path=None)
        vtt_path = self._vtt_sources.get(session_id)
        if vtt_path is not None:
            return MinutesRoute(session_id=session_id, vtt_path=vtt_path)
        return None

    # --- 議事録生成 ------------------------------------------------------------

    def start(self, session: SessionSummary, *, claude: bool) -> None:
        """選択中セッションの議事録生成を開始する（命名ゲート待ちならモーダルを挟む）。"""
        if session.stage is Stage.MINUTES_DONE:
            self._app.log_line(f"{session.session_id} は議事録生成済みです。")
            return
        route = self.resolve_route(session.session_id)
        if route is None:
            # 取込由来（FR-17/18）は録音マニフェストを持たず paired 経路では処理できない。
            # 入力 VTT パスは TUI 再起動で失われるため、その場合は取込からやり直してもらう。
            self._app.log_line(
                f"{session.session_id} は取込（VTT/mp4）由来で、TUI が入力ファイルを保持していません。"
                "「取込」ボタンで同じファイルを再指定して続けてください。"
            )
            return
        # 明示操作での起動は自動再開の回数をリセットする（前回の打ち切りを引き継がない）。
        self._auto_resume_counts.pop(route.session_id, None)
        if session.stage is Stage.NAMING_REQUIRED:
            # 話者名が空欄のままだと議事録の話者が spk_n で残る。記入モーダルを挟んで再開する。
            self.open_speaker_names_modal(route, claude=claude)
            return
        self.launch(route, claude=claude)

    def launch(self, route: MinutesRoute, *, claude: bool) -> None:
        """経路に応じた議事録パイプラインをワーカーで起動する（戻り値は (session_id, rc) で統一）。"""
        if self._app.worker_active(route.worker_group):
            self._app.log_line("議事録パイプラインを実行中です。完了までお待ちください。")
            return
        app = self._app
        session_id = route.session_id
        vtt_path = route.vtt_path
        if vtt_path is None:
            # 完了時に「どのセッションが終わったか」を選択状態でなくワーカー戻り値で判定するため
            # session_id を rc と一緒に返す（実行中に別行を選択されても取り違えない）。
            def work() -> tuple[str, int]:
                return (
                    session_id,
                    pipeline.run_minutes_pipeline(
                        app.cfg, session_id, claude=claude, run=app.subprocess_runner, emit=app.log_from_thread
                    ),
                )

        else:

            def work() -> tuple[str, int]:
                return pipeline.run_vtt_pipeline(
                    app.cfg, vtt_path, claude=claude, run=app.subprocess_runner, emit=app.log_from_thread
                )

        app.start_session_worker(
            work,
            group=route.worker_group,
            session_id=session_id,
            # 経路（paired / vtt）に関わらず「議事録生成」として扱う（取込と混同させない）。
            activity=Activity.MINUTES,
            name=worker_name(claude),
        )

    # --- 話者名ゲート（BR-NAME-01/02・FR-H2-03） --------------------------------

    def open_speaker_names_modal(self, route: MinutesRoute, *, claude: bool) -> None:
        """speaker_names.json の記入待ちをモーダルで埋めてからパイプラインを再開する。

        記入待ちが無ければモーダルを出さずそのまま再開する。
        """
        path = self._app.cfg.session_out_dir(route.session_id) / "speaker_names.json"
        try:
            data = naming.load(path)
        except ValueError as exc:
            self._app.log_line(f"エラー: {exc}")
            return
        prompts = naming.pending_prompts(data)
        if not prompts:
            self.launch(route, claude=claude)
            return

        def on_result(names: dict[str, str] | None) -> None:
            if names is None:
                self._app.log_line("話者名の記入を中止しました（議事録は未生成です）。「議事録作成」で再開できます。")
                return
            count = naming.applied_count(data, names)
            if count:
                try:
                    naming.save(path, naming.apply_names(data, names))
                except ValueError as exc:
                    self._app.log_line(f"エラー: {exc}")
                    return
                self._app.log_line(f"話者名を {count} 件記入しました（実名はログに出しません）。")
            else:
                self._app.log_line("話者名の記入はありません（spk_n のラベルのまま続行します）。")
            self.launch(route, claude=claude)

        self._app.push_screen(
            SpeakerNamesModal(f"話者名の記入: {route.session_id}", prompts),
            callback=on_result,
        )

    def maybe_resume_after_naming(self, session_id: str, *, claude: bool) -> bool:
        """命名ゲートで停止した直後なら、記入モーダルを開いて自動再開する（できたら True）。

        1セッションあたり `_MAX_AUTO_RESUME` 回まで。上限到達・経路不明の場合は False を返し、
        呼び出し側が従来の次段案内を出す。
        """
        if runner.detect_stage(self._app.cfg, session_id) is not Stage.NAMING_REQUIRED:
            return False
        if self._auto_resume_counts.get(session_id, 0) >= _MAX_AUTO_RESUME:
            return False
        route = self.resolve_route(session_id)
        if route is None:
            return False
        self._auto_resume_counts[session_id] = self._auto_resume_counts.get(session_id, 0) + 1
        self.open_speaker_names_modal(route, claude=claude)
        return True

    # --- 取込（VTT=FR-17 / mp4=FR-18。拡張子で経路を振り分ける） -----------------

    def import_file(self, path: Path, *, claude: bool) -> None:
        """取込を開始する。拡張子で mp4 取込段と VTT 直接取込を振り分ける。"""
        if not path.is_file():
            self._app.log_line(f"ファイルが見つかりません: {path}")
            return
        if path.suffix.lower() == _MP4_SUFFIX:
            self._import_mp4(path, claude=claude)
            return
        if self._app.worker_active(VTT_WORKER_GROUP):
            self._app.log_line("取込を実行中です。完了までお待ちください。")
            return
        # 命名ゲートからの再開に元の VTT パスが要るため記憶する（セッションID＝ファイル名 stem）。
        session_id = runner.vtt_session_id(path)
        self._vtt_sources[session_id] = path
        app = self._app
        app.start_session_worker(
            lambda: pipeline.run_vtt_pipeline(
                app.cfg, path, claude=claude, run=app.subprocess_runner, emit=app.log_from_thread
            ),
            group=VTT_WORKER_GROUP,
            session_id=session_id,
            activity=Activity.IMPORT,
            name=worker_name(claude),
        )

    def _import_mp4(self, mp4_path: Path, *, claude: bool) -> None:
        """録画 mp4 の取込（FR-18）→ VTT 経路（FR-17）へ接続するパイプラインを起動する。

        再開に使うのは生成後の VTT（mp4 ではなく）。mp4 を再指定しても変換済み VTT があれば
        Transcribe を再実行しないが、記憶するのは変換結果のパスにしておく。
        """
        if self._app.worker_active(MP4_WORKER_GROUP):
            self._app.log_line("mp4 取込を実行中です。完了までお待ちください。")
            return
        session_id = runner.vtt_session_id(mp4_path)
        self._vtt_sources[session_id] = runner.mp4_vtt_output_path(mp4_path)
        app = self._app
        app.start_session_worker(
            lambda: pipeline.run_mp4_pipeline(
                app.cfg, mp4_path, claude=claude, run=app.subprocess_runner, emit=app.log_from_thread
            ),
            group=MP4_WORKER_GROUP,
            session_id=session_id,
            activity=Activity.IMPORT,
            name=worker_name(claude),
        )
