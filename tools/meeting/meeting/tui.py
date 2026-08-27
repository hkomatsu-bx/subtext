"""会議ハーネスの TUI（`meeting tui`）。

セッション一覧・録音開始/停止・議事録生成(Bedrock/Claude)・取込(VTT/mp4)・Slack投稿を画面操作で
行う。ライブ字幕の開始は対象外（引き続き `meeting live` をコマンドで叩く）。ロジックは
runner/pipeline/slack/naming にそのまま委ね、ここは薄いフロントエンドに徹する（課金見積・Slack
投稿前プレビューの内容は CLI と共通）。

この画面の責務は「キー/ボタンの受け取り・一覧と状態の描画・ログ出力・ワーカーの起動」に絞り、
残りは同じパッケージの 4 モジュールへ分けている。

  - `tui_view`     … 表示部品（一覧・段・ロゴ・パレット）と段の状態計算（純粋）
  - `tui_modals`   … 確認・パス入力・Slack プレビュー・話者名記入のモーダル
  - `tui_recording`… 録音プロセスの起動と停止（Textual 非依存）
  - `tui_minutes`  … 取込 → 命名ゲート → 議事録の進行と経路の記憶

Transcribe/Bedrock 呼び出しや Slack HTTP、録音 exe の起動は同期/長時間のブロッキング処理の
ため、`run_worker(thread=True)` でワーカースレッド実行し UI をブロックしない。ワーカー
スレッドから UI（ログ表示）を更新する際は `call_from_thread` でメインスレッドへマーシャル
する（Textual のウィジェットはスレッドセーフではないため。`log_from_thread` を使う）。

**ワーカーは必ず `exit_on_error=False` で起動する**。Textual の既定（True）ではワーカー内の
例外がそのままアプリを落とし、クラッシュ画面が各フレームの locals をダンプする。そこには
Slack Bot Token のような秘密が含まれるため、画面に出してはならない（BR-SEC-01・NFR-SEC-04）。
False にすると例外は `WorkerState.ERROR` として `on_worker_state_changed` に届き、
各 `_on_*_worker_done` が 1 行のエラーとしてログへ落とす（TUI は起動したまま）。
実例: 存在しないチャンネル ID を入力すると Slack API が `channel_not_found` を返し、
`slack.post_message` が送出する例外でアプリごと落ちてトークンが露出していた。

モーダルは `push_screen(callback=...)` で結果を受け取り、承認後の副作用（Slack 投稿・削除・
パイプライン起動）だけを実行する（`push_screen_wait` はワーカー内でしか呼べない上、ボタン
ハンドラ内で使うとモーダル解消のタイミングでワーカー自体がキャンセルされる Textual の
内部挙動があるため使わない）。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Callable

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.content import Content
from textual.timer import Timer
from textual.widgets import Button, DataTable, Footer, Input, Log, Static
from textual.worker import Worker, WorkerState

from meeting import edit as edit_mod
from meeting import ledger, pipeline, runner, slack
from meeting import materials as materials_mod
from meeting.config import MeetingConfig
from meeting.runner import Runner as SubprocessRunner
from meeting.runner import SessionSummary, Stage
from meeting.tui_modals import ConfirmModal, MaterialsInputModal, PathInputModal, SlackPostModal
from meeting.tui_minutes import (
    CLAUDE_WORKER_NAME,
    MINUTES_WORKER_GROUP,
    MP4_WORKER_GROUP,
    VTT_WORKER_GROUP,
    MinutesFlow,
)
from meeting.tui_recording import PopenFactory, RecordingController
from meeting.tui_view import (
    BLINK_INTERVAL_SEC,
    COLUMN_KEYS,
    COLUMNS,
    STEP_INDEXES,
    STEP_KEYS,
    STEP_LABELS,
    Activity,
    Logo,
    MeetingCommands,
    RecordMinutesInput,
    SessionTable,
    Step,
    auth_text,
    column_widths,
    format_seconds,
    merge_recording_session,
    number_cell,
    pipeline_meta,
    pipeline_states,
    stage_cell,
    strip_path_quotes,
)

_SLACK_WORKER_GROUP = "slack"
_RECORD_WORKER_GROUP = "record"
_AUTH_WORKER_GROUP = "auth"
_EDIT_WORKER_GROUP = "edit"  # ②議事録編集（外部エディタはブロッキングなのでスレッドで動かす）

# 実行中をキャプションで示すボタン（待機ラベルは compose がそのまま使う）。
_MINUTES_LABEL = "議事録作成 (m)"
_MINUTES_RUNNING_LABEL = "議事録作成中 (m)"
_IMPORT_LABEL = "取込 VTT/mp4 (i)"
_IMPORT_RUNNING_LABEL = "取込中 (i)"
# (ウィジェットID, 待機ラベル, 実行中ラベル, 対応する処理)。呼び出しごとに組み直さない。
_ACTION_BUTTONS = (
    ("minutes-bedrock", _MINUTES_LABEL, _MINUTES_RUNNING_LABEL, Activity.MINUTES),
    ("import-vtt", _IMPORT_LABEL, _IMPORT_RUNNING_LABEL, Activity.IMPORT),
)


class MeetingApp(App[None]):
    """会議ハーネスの TUI 本体。"""

    CSS_PATH = "app.tcss"
    COMMANDS = App.COMMANDS | {MeetingCommands}
    BINDINGS = [
        ("r", "record_toggle", "録音"),
        ("i", "import_vtt", "取込"),
        ("m", "minutes", "議事録"),
        ("e", "edit_minutes", "議事録編集"),
        ("d", "import_materials", "資料投入"),
        ("s", "slack", "Slack共有"),
        ("x", "delete_session", "削除"),
        ("a", "check_auth", "AWS確認"),
        ("ctrl+r", "refresh", "再読込"),
        ("ctrl+l", "toggle_log", "ログ"),
        ("q", "quit", "終了"),
    ]

    def __init__(
        self,
        cfg: MeetingConfig,
        *,
        run: SubprocessRunner,
        post: slack.Poster | None = None,
        popen: PopenFactory = subprocess.Popen,
    ) -> None:
        super().__init__()
        self._cfg = cfg
        self._run = run
        self._post = post
        # 録音プロセスの起動・停止（tui_recording）と議事録フローの進行（tui_minutes）を分離する。
        self._recorder = RecordingController(cfg, run=run, popen=popen)
        self._minutes = MinutesFlow(self)
        self._sessions: list[SessionSummary] = []
        # セッションID昇順での通し番号（都度算出。永続化しない＝増減で番号がズレる）。
        self._session_numbers: dict[str, int] = {}
        self._selected: str | None = None
        # Slack投稿済みセッションID（プロセス内一時状態。永続化はしない＝再起動で未共有に戻る）。
        self._shared_sessions: set[str] = set()
        # 進行中の処理（ワーカーグループ → (セッションID, 種別)）。録音は含めない
        # （RecordingController が持つ）。セッションIDを鍵にしないのは、ワーカーが例外終了すると
        # 戻り値（(session_id, rc)）が無く解除できないため。グループは Worker.StateChanged に
        # 必ず載るので確実に消せる。
        self._activities: dict[str, tuple[str, Activity]] = {}
        # AWS 資格情報の確認結果（None = 未確認/確認中）。
        self._auth: runner.AuthStatus | None = None
        # 処理中マーカーの点滅位相（True = 点灯）と、位相を進めるタイマー（テストでは止める）。
        self._blink_lit = True
        self._blink_timer: Timer | None = None
        self._log_expanded = False

    @property
    def cfg(self) -> MeetingConfig:
        """解決済み設定（議事録フローから参照する）。"""
        return self._cfg

    @property
    def subprocess_runner(self) -> SubprocessRunner:
        """subprocess 起動の seam（議事録フローがパイプラインへ渡す）。"""
        return self._run

    def compose(self) -> ComposeResult:
        yield Logo(self._cfg.repo_root)
        with Horizontal(id="status"):
            # セッションIDは VTT/mp4 のファイル名由来のため markup=False（ConfirmModal の docstring 参照）。
            yield Static(id="status-session", markup=False)
            yield Static(classes="spacer")
            # AWS の profile/region（外部設定由来のため markup=False。値は秘密でない＝BR-H2-AUTH-04）。
            yield Static(id="status-aws", markup=False)
            yield Static(id="status-cost")
            yield Static(id="status-state", classes="state")
        with Horizontal(id="pipeline"):
            for i, (key, index, label) in enumerate(
                zip(STEP_KEYS, STEP_INDEXES, STEP_LABELS, strict=True)
            ):
                if i:
                    yield Static("›", classes="step-sep")
                yield Step(key, index, label)
        with Horizontal(id="actions"):
            yield RecordMinutesInput(
                id="record-minutes", value=str(runner.DEFAULT_RECORD_MINUTES), placeholder="分"
            )
            yield Button("録音開始 (r)", id="record-toggle", variant="success")
            yield Button(_MINUTES_LABEL, id="minutes-bedrock", variant="primary")
            yield Button("議事録編集 (e)", id="edit-minutes")
            yield Button("資料投入 (d)", id="import-materials")
            yield Button("Slack投稿 (s)", id="slack")
            yield Static("│\n│\n│", classes="sep")
            yield Button("再読込 (^R)", id="refresh")
            # id は VTT 専用だった当時の名残（キーバインド・既存テストが参照するため維持）。
            yield Button(_IMPORT_LABEL, id="import-vtt", variant="success")
            yield Static("│\n│\n│", classes="sep")
            yield Button("セッション削除 (x)", id="delete-session", variant="error")
        # cursor_foreground_priority="renderable": カーソル行でもセル自身の色を残す。既定（"css"）だと
        # `.datatable--cursor` の color がセルの色を上書きし、**選択行のマーカーだけ赤が消える**。
        # 録音開始時は当の録音セッションを選択状態にするため、既定のままでは肝心の行で色が出ない。
        yield SessionTable(id="table", cursor_type="row", cursor_foreground_priority="renderable")
        yield Log(id="log", highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        self.action_refresh()  # 列も action_refresh が張り直す（幅を内容から決めるため）
        table = self.query_one("#table", DataTable)
        table.focus()
        # 処理中マーカーの点滅（動いているセッションが無い間は描画しない）。
        self._blink_timer = self.set_interval(BLINK_INTERVAL_SEC, self._toggle_blink)
        # 会議前に失効を知らせる（FR-H2-11）。起動はブロックしない。
        self._start_auth_check()

    def action_toggle_log(self) -> None:
        """^L: セッション一覧を隠してログを全画面表示（再度押すと元に戻る）。"""
        self._log_expanded = not self._log_expanded
        self.query_one("#table", DataTable).display = not self._log_expanded

    # --- セッション一覧 ---------------------------------------------------

    def action_refresh(self) -> None:
        # runner は「意味的に新しい順」で返す（既定選択に使う）。一覧はセッションID昇順（表示用）。
        # 番号は都度算出のため、増減で他行の番号がズレ得る。
        # 録音中セッションは manifest 未生成で母集団に入らないため、表示側で合成する。
        by_recency = merge_recording_session(runner.list_sessions(self._cfg), self._recorder.session_id)
        self._sessions = sorted(by_recency, key=lambda s: s.session_id)
        self._session_numbers = {s.session_id: i + 1 for i, s in enumerate(self._sessions)}
        rows = [self._build_row(s) for s in self._sessions]
        table = self.query_one("#table", DataTable)
        # 列も張り直す。`Content` セルは Textual の自動幅計算で 1 セル扱いになり、幅を渡さないと
        # 列がヘッダ幅のまま潰れてセッションIDが切れる（`column_widths` の docstring 参照）。
        table.clear(columns=True)
        for label, key, width in zip(COLUMNS, COLUMN_KEYS, column_widths(rows), strict=True):
            table.add_column(label, key=key, width=width)
        for s, row in zip(self._sessions, rows, strict=True):
            table.add_row(*row, key=s.session_id)
        if self._selected is not None and self._selected not in self._session_numbers:
            self._selected = None  # 消えたセッションを選択したままにしない（既定選択へ戻す）
        if self._selected is None and by_recency:
            # 表示順（名前昇順）とは独立に、意味的に最新（最終更新時刻）を既定選択にする。
            # 名前順で選ぶと `live_captions` 等の取込セッションが常に勝つ（runner._recency_key 参照）。
            self._selected = by_recency[0].session_id
        self._sync_cursor_to_selection(table)
        self.log_line(f"セッション一覧を更新しました（{len(self._sessions)} 件）。")
        self._refresh_live_state()

    def _build_row(self, session: SessionSummary) -> tuple[Content | str, ...]:
        """一覧 1 行のセルを組む（列の順は COLUMNS と対応）。

        セッションIDは `Content` で渡す（DataTable は `str` セルをマークアップとして解釈し、
        `meeting-[draft]` のような ASCII 角括弧を黙って落とす。一覧から名前が消えると操作者が
        対象を選べない。詳細は ConfirmModal の docstring）。
        """
        activity = self._activity_of(session.session_id)
        return (
            number_cell(self._session_numbers[session.session_id], activity, lit=self._blink_lit),
            Content(session.session_id),
            stage_cell(session, activity, runner.STAGE_LABELS),
            session.status,
            format_seconds(session.self_sec),
            format_seconds(session.others_sec),
        )

    def _sync_cursor_to_selection(self, table: DataTable[str]) -> None:
        """表のカーソルを `_selected` の行へ移す（見えている選択と操作対象を一致させる）。

        これをしないと選択が行 0 に巻き戻る。`clear()` 直後の最初の `add_row` は DataTable が
        RowHighlighted(0) を **post_message で** 投げるため、`action_refresh` から復帰した後に
        ハンドラが走り `_selected` を上書きしてしまう（代入した直後に外から潰される）。
        取込直後は特に危険で、VTT/mp4 のセッションIDはファイル名 stem のため数字の録音IDより
        後ろに並び、行 0 になり得ない＝必ず別セッションへ選択が移る。その状態で共有すれば
        別会議の議事録を Slack へ投稿し、議事録を再実行すれば別録音へ課金する。
        """
        if self._selected is None:
            return
        index = next((i for i, s in enumerate(self._sessions) if s.session_id == self._selected), None)
        if index is not None:
            table.move_cursor(row=index)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key.value is not None:
            self._selected = str(event.row_key.value)
            self._refresh_live_state()

    def _current_session(self) -> SessionSummary | None:
        return next((s for s in self._sessions if s.session_id == self._selected), None)

    # --- 進行中の処理（一覧のマーカー） -------------------------------------

    def _activity_of(self, session_id: str) -> Activity | None:
        """そのセッションで進行中の処理（無ければ None）。

        録音はプロセスの生存から導く（自己修復的。ワーカーの登録漏れで残らない）。それ以外は
        `_activities` に登録された内容を引く。同一セッションで複数の処理が同時に走ることは
        既存のガードで起きない（録音中セッションは議事録経路へ入れない・議事録済みのセッションは
        再生成しない）ため、最初に見つかった 1 つを返す。
        """
        if self._recorder.session_id == session_id:
            return Activity.RECORDING
        return next(
            (activity for active, activity in self._activities.values() if active == session_id),
            None,
        )

    def _running_activities(self) -> set[Activity]:
        """いま動いている処理の種別（ボタンのキャプション判定に使う）。"""
        activities = {activity for _session_id, activity in self._activities.values()}
        if self._recorder.session_id is not None:
            activities.add(Activity.RECORDING)
        return activities

    def start_session_worker(
        self, work: Callable[[], object], *, group: str, session_id: str, activity: Activity, name: str
    ) -> None:
        """セッションに紐づくワーカーを起動し、一覧とボタンへ処理中として出す。

        「ワーカー起動＝登録」を 1 か所に閉じる（経路ごとに登録を書くと必ずどこかで漏れる）。
        解除は `on_worker_state_changed` がグループ単位で行う。

        `activity` は**呼び出し側が渡す**。ワーカーグループから導けないためである（取込由来
        セッションで議事録生成を押すと `--mode vtt`＝取込と同じグループで走るので、グループを
        根拠にすると議事録生成中に「取込中」と表示してしまう）。
        """
        self._activities[group] = (session_id, activity)
        self._blink_lit = True  # 現れた瞬間は点灯から始める（消灯位相だと待機の丸に見える）
        self.run_worker(work, thread=True, group=group, name=name, exit_on_error=False)
        self._refresh_activity_cells()
        self._refresh_action_buttons()

    def _toggle_blink(self) -> None:
        """処理中マーカーの点滅位相を進める（`BLINK_INTERVAL_SEC` ごとに呼ばれる）。

        動いているセッションが無いときは何もしない（待機中に一覧を再描画し続けない）。
        """
        if not self._activities and self._recorder.session_id is None:
            return
        self._blink_lit = not self._blink_lit
        self._refresh_activity_cells()

    def _refresh_activity_cells(self) -> None:
        """一覧の `#`／`段` セルだけを更新する（`action_refresh` の全再読込を避ける）。

        全再読込にするとログへ「セッション一覧を更新しました」が毎回出て、進行中の処理の
        ログが流れてしまう。
        """
        table = self.query_one("#table", DataTable)
        for session in self._sessions:
            if session.session_id not in table.rows:
                continue  # 更新中に一覧から消えた行（削除直後など）
            activity = self._activity_of(session.session_id)
            number = self._session_numbers.get(session.session_id)
            # 列幅は action_refresh が内容から決めて固定するため、ここで再計測させない
            # （update_width=True は列ごとの全セル再計測を招くだけで、固定幅の列には効かない）。
            if number is not None:
                table.update_cell(
                    session.session_id,
                    "number",
                    number_cell(number, activity, lit=self._blink_lit),
                    update_width=False,
                )
            table.update_cell(
                session.session_id,
                "stage",
                stage_cell(session, activity, runner.STAGE_LABELS),
                update_width=False,
            )

    # --- ステータスバー / パイプライン段（#status・#pipeline） -----------------

    def _refresh_live_state(self) -> None:
        self._refresh_status()
        self._refresh_pipeline()
        self._refresh_record_button()
        self._refresh_action_buttons()
        self._refresh_activity_cells()

    def _refresh_action_buttons(self) -> None:
        """議事録・取込ボタンのキャプションを実行状態に合わせる。

        無効化はしない（既存のガードが理由をログへ返すため、淡色化してキャプションを読みにくく
        する方が損である）。判定はワーカーグループでなく処理の種別で行う（`start_session_worker`）。
        """
        running = self._running_activities()
        for button_id, label, running_label, activity in _ACTION_BUTTONS:
            button = self.query_one(f"#{button_id}", Button)
            is_running = activity in running
            button.label = running_label if is_running else label
            button.set_class(is_running, "-running")

    def _refresh_record_button(self) -> None:
        button = self.query_one("#record-toggle", Button)
        recording = self._is_recording()
        button.label = "停止 (r)" if recording else "録音開始 (r)"
        button.variant = "warning" if recording else "success"
        button.set_class(recording, "-recording")

    def _is_recording(self) -> bool:
        return self._recorder.is_recording()

    def worker_active(self, group: str) -> bool:
        """指定グループのワーカーが起動待ち/実行中か（同種操作の二重起動防止）。

        `run_worker` はメインスレッドでワーカーを即座に PENDING 登録するため、実処理の開始前
        （録音のビルド待ち・議事録の見積中など）でもこの判定で捕捉でき、`exclusive` の「先行を
        キャンセル」に頼らずに二重起動を弾ける。外部副作用（AWS課金・Slack投稿）のある操作は
        先行をキャンセルしても thread ワーカーの実処理は止まらないため、キャンセルでなく拒否する。
        """
        return any(
            worker.group == group and worker.state in (WorkerState.PENDING, WorkerState.RUNNING)
            for worker in self.workers
        )

    def _refresh_status(self) -> None:
        session = self._current_session()
        if session is None:
            session_label = "(未選択)"
        else:
            number = self._session_numbers.get(session.session_id)
            session_label = f"#{number} {session.session_id}" if number is not None else session.session_id
        self.query_one("#status-session", Static).update(session_label)
        aws_state = self.query_one("#status-aws", Static)
        aws_state.update(auth_text(self._auth))
        aws_state.set_class(self._auth is not None and not self._auth.ok, "-warn")
        entries = ledger.load(self._cfg.ledger_path)
        month = ledger.month_of(ledger.now_iso())
        total = ledger.monthly_total(entries, month)
        self.query_one("#status-cost", Static).update(f"今月 ${total:.2f} / ${self._cfg.thresholds.monthly_usd:.2f}")
        recording = self._is_recording()
        state = self.query_one("#status-state", Static)
        state.update("● 録音中" if recording else "○ 待機中")
        state.set_class(recording, "-recording")

    def _refresh_pipeline(self) -> None:
        session = self._current_session()
        stage = session.stage if session is not None else None
        # 段は選択中セッションの状態を示すため、録音中の判定も「選択中セッションが録音中か」に絞る
        # （別セッションの録音で全セッションの段表示が録音中になるのを防ぐ）。
        activity = self._activity_of(session.session_id) if session is not None else None
        recording = activity is Activity.RECORDING
        shared = session is not None and session.session_id in self._shared_sessions
        states = pipeline_states(stage, recording=recording, shared=shared)
        metas = pipeline_meta(
            states,
            stage,
            recording=recording,
            imported=session is not None and session.self_sec is None,
            activity=activity,
        )
        for key, state, meta in zip(STEP_KEYS, states, metas, strict=True):
            self.query_one(f"#step-{key}", Step).set_state(state, meta)

    # --- ログ（メインスレッド書込 / ワーカースレッド安全 emit） -------------

    def log_line(self, line: str) -> None:
        """ログへ書く。改行を含む場合は行ごとに分けて書く。

        `Log` は折り返しをしないため、長い 1 行はウィジェット幅で切れて右端から先が
        横スクロールしないと読めない。エラーの対処のような「読ませたい後半」が切れるのを
        避けるため、呼び出し側は改行で区切って渡し、ここで行へ割る。
        """
        log = self.query_one("#log", Log)
        for part in line.split("\n"):
            log.write_line(part)

    def log_from_thread(self, line: str) -> None:
        """ワーカースレッドから呼ばれる emit。call_from_thread でメインスレッドへ渡す。"""
        self.call_from_thread(self.log_line, line)

    # --- ボタン操作 ---------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "record-toggle":
            self.action_record_toggle()
        elif bid == "refresh":
            self.action_refresh()
        elif bid == "minutes-bedrock":
            self.action_minutes()
        elif bid == "import-vtt":
            self.action_import_vtt()
        elif bid == "edit-minutes":
            self.action_edit_minutes()
        elif bid == "import-materials":
            self.action_import_materials()
        elif bid == "slack":
            self.action_slack()
        elif bid == "delete-session":
            self.action_delete_session()

    def action_record_toggle(self) -> None:
        """R: 停止中なら録音開始、録音中なら停止（ボタン1つ・キー1つに統合）。"""
        if self._is_recording():
            self._stop_recording()
        else:
            self._start_record()

    def _stop_recording(self) -> None:
        self.log_line(f"停止ファイルを作成しました: {self._recorder.request_stop()}")

    # --- 録音開始（subprocess.Popen で非ブロッキング起動） ---------------------

    def _start_record(self) -> None:
        # プロセス起点（実行中の録音）とワーカー起点（ビルド待ちで未だ Popen 前）の両方を弾く。
        # 後者を見ないと、dotnet ビルド中に 'r' を連打すると recorder が二重起動し WASAPI を奪い合う。
        if self._is_recording() or self.worker_active(_RECORD_WORKER_GROUP):
            self.log_line("既に録音中です。停止してから再度開始してください。")
            return
        raw_minutes = self.query_one("#record-minutes", Input).value.strip()
        try:
            minutes = int(raw_minutes)
            if minutes <= 0:
                raise ValueError
        except ValueError:
            self.log_line(f"録音分数が不正です: {raw_minutes!r}（正の整数を指定）")
            return
        # exclusive にはしない（録音は先行をキャンセルでなく上のガードで拒否する）。
        self.run_worker(
            lambda: self._recorder.run_until_exit(
                minutes,
                emit=self.log_from_thread,
                on_started=lambda: self.call_from_thread(self._refresh_live_state),
                on_session_id=lambda session_id: self.call_from_thread(self._on_recording_session_id, session_id),
            ),
            thread=True,
            group=_RECORD_WORKER_GROUP,
            exit_on_error=False,
        )

    def _on_recording_session_id(self, session_id: str) -> None:
        """録音セッションIDが分かった時点で一覧を更新し、そのセッションを選択する。

        選択を移さないと前のセッションが選ばれたまま残り、停止直後に議事録生成を押すと
        別の録音へ課金する（`action_refresh` は非 None の選択を上書きしないので、先に代入する）。
        """
        self._selected = session_id
        self._blink_lit = True  # 録音マーカーも点灯から始める
        self.action_refresh()

    async def on_unmount(self) -> None:
        """TUI 終了時、録音が進行中なら必ず止める（知らずに録音が継続するのを防ぐ）。

        待機はブロッキングのため、イベントループ上で直接 wait すると終了操作で UI 全体が
        数十秒フリーズする。`asyncio.to_thread` で別スレッドへ退避し、ループを塞がない。
        """
        if not self._recorder.is_recording():
            return
        await asyncio.to_thread(self._recorder.stop_and_wait)

    def action_minutes(self) -> None:
        """m: 議事録作成（Bedrock）。Claude経路はコマンドパレットから。"""
        self._start_minutes(claude=False)

    def _start_minutes(self, *, claude: bool) -> None:
        session = self._current_session()
        if session is None:
            self.log_line("セッションが選択されていません。")
            return
        if self._recorder.session_id == session.session_id:
            # 録音中セッションは manifest を持たないため、そのまま渡すと「取込由来で入力ファイルを
            # 保持していません」という事実と違う案内になる。
            self.log_line(f"{session.session_id} は録音中です。停止してから議事録を作成してください。")
            return
        self._minutes.start(session, claude=claude)

    # --- 議事録編集（②議事録編集・Phase 1：外部エディタ） -----------------------

    def action_edit_minutes(self) -> None:
        """e: 議事録(minutes.md)を外部エディタで編集する。まだ生成されていなければ拒否する。

        エディタ起動はブロッキング（ユーザーが閉じるまで戻らない）のため、録音起動と同様に
        スレッドワーカーで実行し UI を止めない。
        """
        session = self._current_session()
        if session is None:
            self.log_line("セッションが選択されていません。")
            return
        if self.worker_active(_EDIT_WORKER_GROUP):
            self.log_line("議事録を編集中です。エディタを閉じてから再度実行してください。")
            return
        session_id = session.session_id
        minutes_path = self._cfg.session_out_dir(session_id) / "minutes.md"
        if not minutes_path.is_file():
            self.log_line(f"{session_id} はまだ議事録がありません（先に議事録を生成してください）。")
            return

        def work() -> tuple[str, tuple[str, ...]]:
            _markdown, missing = edit_mod.edit(minutes_path)
            return session_id, missing

        self.start_session_worker(
            work, group=_EDIT_WORKER_GROUP, session_id=session_id, activity=Activity.EDIT, name="edit"
        )

    # --- 付帯資料の投入（③付帯資料） --------------------------------------------

    def action_import_materials(self) -> None:
        """d: 付帯資料（.txt/.md/.pdf/.pptx）を投入するモーダルを開く。

        解決・コピーは同期的なローカル IO のみ（AWS を呼ばない）ため、ワーカーへ退避しない。
        実際の抽出・要約プロンプトへの反映は次回の議事録生成（Unit B）で行われる。
        """
        session = self._current_session()
        if session is None:
            self.log_line("セッションが選択されていません。")
            return
        session_id = session.session_id

        def on_result(raw: str | None) -> None:
            if raw is None:
                self.log_line("資料投入を中止しました。")
                return
            lines = materials_mod.parse_input_lines(raw)
            if not lines:
                self.log_line("入力がありませんでした。")
                return
            resolved, warnings = materials_mod.resolve_paths(lines)
            for warning in warnings:
                self.log_line(f"⚠ {warning}")
            if not resolved:
                self.log_line("投入するファイルがありませんでした。")
                return
            materials_dir = materials_mod.copy_into(self._cfg, session_id, resolved)
            self.log_line(f"{len(resolved)} 件を投入しました: {materials_dir}")
            self.log_line("次回の議事録生成時に抽出され、内容が反映されます。")

        self.push_screen(MaterialsInputModal(f"付帯資料の投入: {session_id}"), callback=on_result)

    # --- 取込（VTT=FR-17 / mp4=FR-18。拡張子で経路を振り分ける） -----------------

    def action_import_vtt(self) -> None:
        """i: 取込（パス入力モーダルを開く。Bedrock経路）。

        メソッド名・ウィジェット ID の `vtt` は VTT 専用だった当時の名残（キーバインドと
        既存テストが参照するため維持）。実際は mp4 も受け付ける。
        """
        self._open_import_vtt_modal(claude=False)

    def _open_import_vtt_modal(self, *, claude: bool) -> None:
        def on_result(raw_path: str | None) -> None:
            if raw_path is None:
                self.log_line("取込を中止しました。")
                return
            if not raw_path:
                self.log_line("ファイルパスが未入力です。")
                return
            self._minutes.import_file(Path(strip_path_quotes(raw_path)), claude=claude)

        self.push_screen(PathInputModal("取込ファイル（VTT / mp4）のパスを入力"), callback=on_result)

    def action_slack(self) -> None:
        """s: Slack投稿モーダルを開く（プレビュー＋投稿先チャンネル入力を1画面で）。"""
        self._start_slack()

    def _start_slack(self) -> None:
        """Slack投稿の前段検証→プレビュー+チャンネル入力モーダル表示。

        投稿自体はモーダルの結果コールバックで行う。`push_screen_wait`（await 待ち）は
        ワーカー内でないと呼べない上、ボタン押下ハンドラ内で使うとモーダル解消のタイミングで
        ワーカーがキャンセルされる（Textual の内部挙動）ため、callback 経由の `push_screen` を使う。
        """
        session = self._current_session()
        if session is None or session.stage is not Stage.MINUTES_DONE:
            self.log_line("Slack 投稿には議事録生成済み（minutes.md）のセッションを選択してください。")
            return
        # トークンは「有無」だけを確認し、値はこのフレームへ束縛しない。メインスレッドで未捕捉例外が
        # 起きると Textual のクラッシュ画面が各フレームの locals をダンプし、トークンが端末へ丸ごと
        # 出てスクロールバックに残る（BR-SEC-01・NFR-SEC-04）。値の解決は投稿ワーカー内まで遅らせる
        # （ワーカーの例外は exit_on_error=False で WorkerState.ERROR に閉じ込められる）。
        if not slack.has_slack_token(self._cfg.repo_root, os.environ):
            self.log_line("エラー: SLACK_BOT_TOKEN が未設定です（環境変数かルート .env に設定）。")
            return

        minutes_path = self._cfg.session_out_dir(session.session_id) / "minutes.md"
        try:
            minutes_md = minutes_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # UnicodeDecodeError は OSError ではない（ValueError 系）。捕り落とすとメインスレッドの
            # 未捕捉例外になりクラッシュ画面へ落ちる。cp932/UTF-16 で保存し直された minutes.md で起きる。
            self.log_line(
                f"エラー: minutes.md を読み込めません（UTF-8 で保存されているか確認してください）: "
                f"{minutes_path}: {exc}"
            )
            return

        session_id = session.session_id
        parent = slack.build_parent(session_id, minutes_md)
        # `post_minutes` と同じ入力（決定事項・ToDo を除いた本文）でプレビューする。minutes_md を
        # そのまま渡すと、実際の投稿内容（重複除去済み・注記付き）とプレビューがずれ、承認した
        # ものと送るものが違う状態になる（CLI の `_confirm_and_post` と同じ理由）。
        body = slack.to_mrkdwn(slack.build_thread_body(minutes_md))
        preview = f"[親メッセージ（要約）]\n{parent}\n\n[スレッド全文]\n{body}"

        def on_result(channel: str | None) -> None:
            if channel is None:
                self.log_line("投稿を中止しました。")
                return
            if not channel:
                self.log_line("エラー: 投稿先チャンネルが未入力です。")
                return
            # 外部公開のため、実行中の投稿はキャンセルでなく拒否する（キャンセルしても thread の
            # 実投稿は止まらず、二重投稿や共有済み記録の取りこぼしを招くため）。
            if self.worker_active(_SLACK_WORKER_GROUP):
                self.log_line("Slack 投稿を実行中です。完了までお待ちください。")
                return
            # モーダルのプレビュー確認＋「投稿する」押下＝人間承認済み。
            self.start_session_worker(
                lambda: self._post_slack_worker(session_id, minutes_md, channel, parent),
                group=_SLACK_WORKER_GROUP,
                session_id=session_id,
                activity=Activity.SHARING,
                name=session_id,
            )

        self.push_screen(
            SlackPostModal("Slack 投稿プレビュー", preview, self._cfg.slack_default_channel),
            callback=on_result,
        )

    def _post_slack_worker(self, session_id: str, minutes_md: str, channel: str, parent: str) -> int:
        """ワーカースレッドで Slack 投稿を実行する（Bot Token の解決もここで行う）。

        トークンをメインスレッドのフレームに残さないため、解決を投稿直前まで遅らせる
        （理由は `_start_slack` のコメント参照, BR-SEC-01）。この関数で例外が出ても
        `exit_on_error=False` によりクラッシュ画面には落ちず、`_on_slack_worker_done` が
        1 行のエラーとしてログへ落とす（例外メッセージにトークンは含めない）。
        """
        token = slack.resolve_slack_token(self._cfg.repo_root, os.environ)
        if not token:
            raise ValueError("SLACK_BOT_TOKEN を解決できませんでした（環境変数かルート .env を確認してください）。")
        return slack.post_minutes(
            session_id,
            minutes_md,
            channel,
            token=token,
            approved=True,
            poster=self._post,
            parent=parent,
            emit=self.log_from_thread,
        )

    # --- AWS 資格情報の確認（FR-H2-11） -----------------------------------------

    def action_check_auth(self) -> None:
        """a: AWS 資格情報を再確認する（再認証そのものはハーネスから起動しない）。"""
        if not self._start_auth_check():
            self.log_line("AWS 認証を確認中です。完了までお待ちください。")
            return
        self.log_line("AWS 認証を確認します（課金なし）...")

    def _start_auth_check(self) -> bool:
        """Unit B に資格情報を確認させる（ワーカー実行。起動と操作をブロックしない）。

        ハーネスは boto3 を持たず Unit B の分類ロジックも import できない（FR-16）ため、
        `subtext-postmeeting --check-auth` の stdout 契約だけで結果を受け取る。

        既に確認中なら False を返す（多重起動の判定はここ 1 か所に置き、案内の文言は
        呼び出し側に任せる）。
        """
        if self.worker_active(_AUTH_WORKER_GROUP):
            return False
        self._auth = None  # 「確認中…」表示へ戻す
        self._refresh_status()
        self.run_worker(
            lambda: runner.run_check_auth(self._cfg, runner=self._run),
            thread=True,
            group=_AUTH_WORKER_GROUP,
            exit_on_error=False,
        )
        return True

    def _on_auth_worker_done(self, event: Worker.StateChanged) -> None:
        if event.state is WorkerState.ERROR:
            # run_check_auth は失敗も戻り値で返すため通常ここへは来ない（想定外の例外の保険）。
            self._auth = runner.AuthStatus(runner.AuthState.ERROR, message=str(event.worker.error))
        else:
            assert event.worker.result is not None  # SUCCESS 時は AuthStatus が必ずある
            self._auth = event.worker.result
        status = self._auth
        where = f"profile={status.profile or '(不明)'} / region={status.region or '(不明)'}"
        if status.ok:
            self.log_line(f"AWS 認証 OK（{where}）。")
        else:
            self.log_line(f"⚠ AWS 認証: {status.label}（{where}）。")
            if status.message:
                self.log_line(status.message)
        self._refresh_status()

    # --- セッション削除 ---------------------------------------------------------

    def action_delete_session(self) -> None:
        """x: セッション削除。"""
        self._start_delete_session()

    def _start_delete_session(self) -> None:
        """選択中セッションの削除。確認モーダル→承認後に削除する（取り消せないため必須）。

        ファイル削除はローカル I/O のみで高速なため、Slack/議事録のような worker 化はしない
        （確認コールバック内で同期的に実行する）。
        """
        session = self._current_session()
        if session is None:
            self.log_line("セッションが選択されていません。")
            return
        busy = self._busy_reason()
        if busy is not None:
            self.log_line(f"削除できません: {busy}")
            return
        session_id = session.session_id
        body = (
            f"セッション {session_id} を削除します。\n\n"
            "削除対象:\n"
            f"  - {self._cfg.session_recording_dir(session_id)}\n"
            f"  - {self._cfg.session_out_dir(session_id)}\n\n"
            "録音 WAV はこのコピーだけです（復元できません）。\n"
            "コスト台帳（cost-ledger.jsonl）の記録は監査のため削除されません。\n"
            "この操作は取り消せません。"
        )

        def on_result(confirmed: bool | None) -> None:
            if not confirmed:
                self.log_line("削除を中止しました。")
                return
            # モーダル表示中に状態は変わり得るため、承認後にもう一度見る。実行中のパイプラインの
            # 足元でファイルを消すと、課金済みの台帳追記が manifest 欠落で失敗して「課金だけ残る」
            # 状態になり、後続の書き込みが出力ディレクトリを作り直して「削除したのに残る」ことになる。
            reason = self._busy_reason()
            if reason is not None:
                self.log_line(f"削除できません: {reason}")
                return
            try:
                runner.delete_session(self._cfg, session_id)
            except OSError as exc:
                self.log_line(f"エラー: セッションを削除できませんでした（{session_id}）: {exc}")
                return
            # 共有済み記憶からも除去する。除去しないと、同一IDを再利用する後続セッション
            # （VTTインポートはファイル名 stem がIDのため再取込で必ず衝突）が未投稿のまま
            # 「共有済み」表示になる。
            self._shared_sessions.discard(session_id)
            # 入力 VTT パスと自動再開回数も同じ理由で捨てる（同一IDの再取込で混ざらないように）。
            self._minutes.forget(session_id)
            if self._selected == session_id:
                self._selected = None
            self.log_line(f"セッション {session_id} を削除しました。")
            self.action_refresh()

        self.push_screen(
            ConfirmModal("セッション削除の確認", body, confirm_label="削除する (y)"),
            callback=on_result,
        )

    def _busy_reason(self) -> str | None:
        """外部副作用のある処理が進行中ならその理由を返す（破壊的操作を拒否するため）。"""
        if self._is_recording() or self.worker_active(_RECORD_WORKER_GROUP):
            return "録音中です。停止してから実行してください。"
        return self._busy_worker_reason()

    def _busy_worker_reason(self) -> str | None:
        """止められないワーカーが進行中ならその理由を返す（録音は含まない）。

        録音は `on_unmount` が stop-file で安全に停止できるが、議事録・取込・Slack 投稿・議事録編集は
        thread ワーカーで動く子プロセス/HTTP/外部エディタのため `cancel_all` では止まらない。
        議事録編集を含めるのは、外部エディタを開いたまま終了すると join 待ちで TUI が無反応になり、
        削除を許すと編集中のファイルを足元から消すことになるためである。
        """
        for group in (MINUTES_WORKER_GROUP, VTT_WORKER_GROUP, MP4_WORKER_GROUP, _SLACK_WORKER_GROUP):
            if self.worker_active(group):
                return "議事録生成・取込・Slack 投稿のいずれかを実行中です（完了を待ってください）。"
        if self.worker_active(_EDIT_WORKER_GROUP):
            return "議事録を編集中です（エディタを閉じてから実行してください）。"
        return None

    async def action_quit(self) -> None:
        """q: 終了。課金・外部公開を伴う処理が進行中なら拒否する。

        thread ワーカーは `cancel_all` では止まらない。ここで終了させると子プロセス
        （Unit B / mp4→VTT）が孤児化して**課金だけが進み**、結果も台帳も残らない。さらに
        インタプリタの終了が thread の join 待ちで最大 `THREAD_JOIN_TIMEOUT`（300 秒）ブロックされ、
        操作者からは「終了できないアプリ」に見える。録音中の終了は従来どおり許す（on_unmount が
        stop-file で graceful に停止し、WAV と manifest を確定する）。

        AWS 認証の確認中も待たせる。課金も外部公開も無い処理だが、`subprocess.run` の待ち
        （`CHECK_AUTH_TIMEOUT_SEC`）は同じく止められないため、ここで終了させると「終了操作の後に
        無反応のまま数十秒」になる。理由を出して待ってもらう方が、黙って固まるより良い。
        """
        busy = self._busy_worker_reason()
        if busy is not None:
            self.log_line(f"終了できません: {busy}")
            return
        if self.worker_active(_AUTH_WORKER_GROUP):
            self.log_line(
                f"終了できません: AWS 認証を確認中です（最大 {runner.CHECK_AUTH_TIMEOUT_SEC:.0f} 秒）。"
                "完了後にもう一度 q を押してください。"
            )
            return
        self.exit()

    # --- ワーカー完了ハンドリング（メインスレッドで呼ばれる） -----------------

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state not in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED):
            return
        # 処理中の登録はグループ単位で必ず解除する（例外終了でも戻り値の有無に依存しない）。
        if self._activities.pop(event.worker.group, None) is not None:
            self._refresh_activity_cells()
            self._refresh_action_buttons()
        if event.state is WorkerState.CANCELLED:
            return  # 以降の完了処理（次段の案内・共有済みの記録）は成功/失敗の経路のみ
        if event.worker.group == _AUTH_WORKER_GROUP:
            self._on_auth_worker_done(event)
        elif event.worker.group == MINUTES_WORKER_GROUP:
            self._on_minutes_worker_done(event)
        elif event.worker.group in (VTT_WORKER_GROUP, MP4_WORKER_GROUP):
            # mp4 取込は末尾で VTT 経路へ合流し、戻り値も (session_id, rc) で同じ形になる。
            self._on_vtt_worker_done(event)
        elif event.worker.group == _SLACK_WORKER_GROUP:
            self._on_slack_worker_done(event)
        elif event.worker.group == _RECORD_WORKER_GROUP:
            self._on_record_worker_done(event)
        elif event.worker.group == _EDIT_WORKER_GROUP:
            self._on_edit_worker_done(event)

    def _on_minutes_worker_done(self, event: Worker.StateChanged) -> None:
        if event.state is WorkerState.ERROR:
            self.log_line(f"議事録パイプラインが例外終了しました: {event.worker.error}")
            return
        # 起動時に固めた (session_id, rc)。選択状態でなくワーカー戻り値で判定する。
        assert event.worker.result is not None  # SUCCESS 時は _start_minutes の戻り値が必ずある
        session_id, rc = event.worker.result
        self.action_refresh()
        if rc != 0:
            # 非ゼロ終了はパイプライン側が既にエラー詳細を emit 済み。次段の案内は出さない。
            return
        claude = event.worker.name == CLAUDE_WORKER_NAME
        # 命名ゲートで止まったなら、案内で終わらせず記入モーダルを開いてそのまま再開する。
        if self._minutes.maybe_resume_after_naming(session_id, claude=claude):
            return
        stage = runner.detect_stage(self._cfg, session_id)
        for line in pipeline.next_steps_lines(stage, session_id, claude):
            self.log_line(line)

    def _on_vtt_worker_done(self, event: Worker.StateChanged) -> None:
        if event.state is WorkerState.ERROR:
            self.log_line(f"取込が例外終了しました: {event.worker.error}")
            return
        assert event.worker.result is not None  # SUCCESS 時は run_vtt_pipeline の戻り値が必ずある
        session_id, rc = event.worker.result
        if rc == 0:
            # 再描画より先に選択を新セッションへ移す（後で更新すると status/pipeline 欄が
            # 旧セッションのまま取り残される）。action_refresh は非 None の選択を上書きしない。
            self._selected = session_id
        self.action_refresh()
        if rc != 0:
            return
        claude = event.worker.name == CLAUDE_WORKER_NAME
        if self._minutes.maybe_resume_after_naming(session_id, claude=claude):
            return
        stage = runner.detect_stage(self._cfg, session_id)
        for line in pipeline.next_steps_lines(stage, session_id, claude, vtt=True):
            self.log_line(line)

    def _on_edit_worker_done(self, event: Worker.StateChanged) -> None:
        if event.state is WorkerState.ERROR:
            self.log_line(f"議事録の編集に失敗しました: {event.worker.error}")
            return
        assert event.worker.result is not None  # SUCCESS 時は work() の戻り値が必ずある
        session_id, missing = event.worker.result
        self.log_line(f"{session_id} の議事録を保存しました。")
        if missing:
            self.log_line(
                "⚠ Slack 親メッセージ（要約）の抽出に使う見出しが欠けています: " + "、".join(missing)
            )
        self.log_line("Slack へ投稿済みの場合、この編集は反映されません（再投稿するとスレッドが増えます）。")

    def _on_slack_worker_done(self, event: Worker.StateChanged) -> None:
        if event.state is WorkerState.ERROR:
            self.log_line(f"Slack 投稿に失敗しました: {event.worker.error}")
            return
        if event.worker.name:
            self._shared_sessions.add(event.worker.name)
        self._refresh_live_state()

    def _on_record_worker_done(self, event: Worker.StateChanged) -> None:
        if event.state is WorkerState.ERROR:
            self.log_line(f"録音処理が例外終了しました: {event.worker.error}")
            self.action_refresh()
            return
        rc = event.worker.result  # int（recorder 終了コード）または None（起動前失敗）
        if rc is None:
            pass  # 起動できず終了（原因はワーカーが既にログ済み）。追加の完了表示はしない。
        elif rc == 0:
            self.log_line("録音が終了しました。")
        elif rc == 1:
            self.log_line(
                "⚠ 録音が異常終了しました (exit=1)。録音中に致命的エラーが発生した可能性があります"
                "（WAV は途中まで部分保存されている場合があります）。"
            )
        elif rc == 2:
            self.log_line("⚠ 録音を開始できませんでした (exit=2)。デバイス/起動時の異常の可能性があります。")
        else:
            self.log_line(f"⚠ 録音が終了コード {rc} で終了しました。")
        self.action_refresh()
