"""TUI の表示部品と、画面へ流す値の純粋計算。

`tui.py` から切り出した「見せ方」の層。段の状態（完了/対象/未着手）とその注記は App の状態に
触らない純粋関数にしてあるため、Pilot を起動せずに単体テストできる。

外部由来の文字列（セッションID・パス）を出す箇所のマークアップ扱いは `tui_modals` の docstring
と同じ制約に従う。
"""

from __future__ import annotations

from enum import Enum
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import TYPE_CHECKING, Sequence, cast

from textual import events
from textual.app import ComposeResult
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Vertical
from textual.content import Content
from textual.types import IgnoreReturnCallbackType
from textual.widgets import DataTable, Input, Static

from meeting.runner import AuthState, AuthStatus, SessionSummary, Stage

if TYPE_CHECKING:  # 実行時 import は循環参照になるため型検査時のみ。
    from meeting.tui import MeetingApp

# セッション一覧（#table）の列。列キーは表示位置に依存せずセルを更新するために使う。
COLUMNS = ("#", "セッション", "段", "録音状態", "self(s)", "others(s)")
COLUMN_KEYS = ("number", "session", "stage", "status", "self-sec", "others-sec")
# パイプライン段表示（#pipeline）。表示順の key / ラベル / 通し番号。
STEP_KEYS = ("record", "import", "minutes", "share")
STEP_LABELS = ("録音", "取込", "議事録", "共有")
STEP_INDEXES = ("01", "02", "03", "04")
# 各 Stage が「次に必要な段」の index を持つ（それより前は完了、それが対象、それより後は未着手）。
_STAGE_NEXT_STEP_INDEX: dict[Stage, int] = {
    Stage.NO_RECORDING: 0,
    Stage.RECORDED: 1,
    Stage.NAMING_REQUIRED: 1,
    Stage.TRANSCRIPT_DONE: 2,
    Stage.MINUTES_DONE: 3,
}
# 「共有」段の index（`shared` 判定の対象）。
_SHARE_STEP_INDEX = 3
# ロゴのマイク部分の色（app.tcss の $mt-rec と同値。Static の markup は TCSS 変数を参照できず
# 直書きになるため、値を変えるときは両方を揃えること）。
MIC_RED = "#e0554b"
# 録音以外の処理中マーカーの色（app.tcss の $mt-accent-soft と同値。同じ理由で直書き）。
ACTIVITY_ACCENT = "#b5abfc"

# 段が「完了」のときに残す注記。段ごとに自分の状態を述べる（対象段にだけ現在段ラベルを
# 載せる方式は、`STAGE_LABELS` が完了を述べる文（「議事録生成済」）であるため、まだ終わって
# いない段の下に出て「その段が済んだ」と読めてしまう）。
_DONE_METAS = ("録音済", "文字起こし済", "議事録生成済", "共有済")
# 取込（VTT/mp4）由来のセッションは録音そのものが無いため、01 の完了注記を差し替える。
_IMPORTED_RECORD_META = "録音なし"
# 段が「対象」のときの注記（＝次に何が必要か）。Stage ごとに 1 つ。
_ACTIVE_METAS: dict[Stage, str] = {
    Stage.NO_RECORDING: "未録音",
    Stage.RECORDED: "文字起こし未実行",
    Stage.NAMING_REQUIRED: "話者名の記入待ち",  # 課金の停止点。消してはならない表示。
    Stage.TRANSCRIPT_DONE: "議事録未生成",
    Stage.MINUTES_DONE: "未共有",
}
# 注記の既定値（未着手の段）。
_TODO_META = "-"


class Activity(str, Enum):
    """セッションで進行中の処理（一覧のマーカーと段の文字に使う）。"""

    RECORDING = "recording"
    MINUTES = "minutes"
    IMPORT = "import"
    SHARING = "sharing"


# マーカー（`#` 列の先頭）の文字と色。録音だけ専用色にする。
_ACTIVITY_MARKERS: dict[Activity, tuple[str, str]] = {
    Activity.RECORDING: ("●", MIC_RED),
    Activity.MINUTES: ("◐", ACTIVITY_ACCENT),
    Activity.IMPORT: ("◐", ACTIVITY_ACCENT),
    Activity.SHARING: ("◐", ACTIVITY_ACCENT),
}
# `段` 列に出す進行中の文字（マーカーだけでは録音と他の処理しか区別できないため）。
_ACTIVITY_STAGE_LABELS: dict[Activity, str] = {
    Activity.RECORDING: "録音中（進行中）",
    Activity.MINUTES: "議事録生成中…",
    Activity.IMPORT: "取込中…",
    Activity.SHARING: "Slack投稿中…",
}
# マーカーを置かない行の埋め（マーカーの有無で通し番号の桁がずれると列全体が動く）。
_NO_MARKER = " "
# 点滅の消灯側の記号。中身を抜いた同じ大きさの丸にし、色は点灯側と同じまま残す（消えるのではなく
# 脈打って見える）。`●`/`◐`/`○` はいずれも 1 セル幅なので、点滅で列幅も桁も動かない。
_BLINK_OFF_MARKER = "○"
# 点滅の周期（秒）。ANSI の blink 属性（SGR 5）は端末側の設定で無視されることがあるため、
# タイマーでセルを差し替える方式にする。
BLINK_INTERVAL_SEC = 0.6
# 録音中セッションの合成行に入れる「録音状態」列の値（manifest の complete/incomplete/vtt と並ぶ）。
RECORDING_STATUS = "録音中"
# 列幅を決めるときに、実データに無くても幅へ含める文字列（列キー → 候補）。
# 段の文字は処理中に**セルだけ**差し替わる（列は張り直さない）ため、固定幅に見込んでおく。
# マーカーは幅 1＋空白で待機行の埋めと同幅なので、行の内容から決まる幅で足りる。
_WIDTH_CANDIDATES: dict[str, tuple[str, ...]] = {
    "stage": tuple(_ACTIVITY_STAGE_LABELS.values()),
}

# ボタンを廃止した Claude 経路（議事録・取込）。ラベル / キー / 説明のペア。
_PALETTE_COMMANDS = (
    (
        "議事録を生成 (Claude)",
        "minutes_claude",
        "選択中セッションの議事録を Claude で生成する（既定は m キーの Bedrock 経路）",
    ),
    (
        "取込 VTT/mp4 (Claude)",
        "vtt_claude",
        "VTT または mp4 を Claude 経路で取り込む（既定は i キーの Bedrock 経路）",
    ),
)


def format_seconds(value: float | None) -> str:
    """録音由来は実測秒数、VTTインポート由来（録音なし）は "-" を返す（純粋）。"""
    return f"{value:.1f}" if value is not None else "-"


def strip_path_quotes(raw: str) -> str:
    """パス入力の前後クォートを剥がす（純粋）。

    Windows の「パスのコピー」（Shift+右クリック）はパスを `"..."` で囲んでクリップボードに
    入れるため、そのまま貼り付けるとクォート込みの文字列が存在しないパスとして扱われてしまう。
    前後が対になった `"`/`'` の場合のみ除去する。
    """
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        return raw[1:-1]
    return raw


def number_cell(number: int, activity: Activity | None, *, lit: bool = True) -> Content:
    """`#` 列のセル（処理中マーカー＋通し番号）を作る（純粋）。

    `str` ではなく `Content` を返す。DataTable は `str` セルをマークアップとして解釈するため
    色を付ける手段が無く、また外部由来の文字列と同じ扱いに揃えたいからである。

    `lit` は点滅の位相。False で記号だけ中抜きにする（色と幅は変えない）。
    """
    if activity is None:
        return Content(f"{_NO_MARKER} {number}")
    marker, color = _ACTIVITY_MARKERS[activity]  # 未定義の処理は KeyError で気付けるようにする
    return Content.assemble((marker if lit else _BLINK_OFF_MARKER, color), f" {number}")


def column_widths(rows: Sequence[Sequence[Content | str]]) -> list[int]:
    """列ごとの表示幅（内容幅・パディングを除く）を返す（純粋）。

    Textual の自動幅計算は `Content` セルを 1 セル幅と見なす（`Content` は
    `__rich_measure__` を持たないため `measure()` が既定値を返す）。その結果、`Content` を
    渡す列はヘッダ幅のまま潰れ、**セッション ID が `20260804-1` のように切れて操作対象を
    区別できなくなる**。そこで幅をこちらで決めて `add_column(width=...)` へ渡す。

    セル差し替えで文字が伸びる列（マーカー・段）は、起こり得る文字列も候補に含める
    （`update_cell` は固定幅の列を広げないため、後から「議事録生成中…」が切れる）。
    """
    return [
        _column_width(index, label, key, rows)
        for index, (label, key) in enumerate(zip(COLUMNS, COLUMN_KEYS, strict=True))
    ]


def _column_width(index: int, label: str, key: str, rows: Sequence[Sequence[Content | str]]) -> int:
    """1 列ぶんの表示幅（ヘッダ・差し替え候補・実データの最大値。純粋）。"""
    candidates = (label, *_WIDTH_CANDIDATES.get(key, ()), *(row[index] for row in rows))
    return max(_cell_width(cell) for cell in candidates)


def _cell_width(cell: Content | str) -> int:
    """セルの表示幅（全角を 2 と数える）。

    幅の計算は Textual の `Content.cell_length` に委ねる（`rich.cells` を直接 import すると
    宣言していない依存に触ることになる。ハーネスの依存は `textual` のみをピン留めしている
    ＝NFR-SEC-05）。
    """
    return cell.cell_length if isinstance(cell, Content) else Content(cell).cell_length


def stage_cell(session: SessionSummary, activity: Activity | None, stage_labels: dict[Stage, str]) -> Content:
    """`段` 列のセル（純粋）。処理中はその内容を出し、そうでなければ段のラベルを出す。

    セッション ID と同じく `Content` で渡す（ラベルに ASCII 角括弧は無いが、列ごとに扱いを
    変えると後から追加した文字列で黙って消える事故が起きるため揃える）。
    """
    if activity is not None:
        return Content(_ACTIVITY_STAGE_LABELS[activity])
    return Content(stage_labels[session.stage])


def merge_recording_session(sessions: list[SessionSummary], recording_id: str | None) -> list[SessionSummary]:
    """録音中セッションを一覧へ合成する（純粋。既にあればそのまま）。

    `manifest.json` は録音の**終了時**に書かれるため、進行中のセッションは
    `runner.list_sessions` の母集団に入らない（録音ディレクトリにも `data/out/` にも痕跡が無い）。
    一覧側の母集団を広げると `resolve_session` の既定セッションが録音途中のものになり、CLI の
    `minutes`／`slack` が manifest 無しのセッションを掴むため、合成はここ（表示側）で行う。

    合成行の段は `NO_RECORDING`（manifest なし）で、表示は `stage_cell` が「録音中」に差し替える。

    **先頭に入れる**。呼び出し側はこの並び（意味的に新しい順）の先頭を既定選択に使うため、
    末尾に足すと「今まさに録っているセッション」が最古扱いになり、選択が外れた場合に
    別セッションが既定になってしまう。
    """
    if recording_id is None or any(s.session_id == recording_id for s in sessions):
        return sessions
    synthetic = SessionSummary(
        session_id=recording_id,
        stage=Stage.NO_RECORDING,
        status=RECORDING_STATUS,
        self_sec=None,
        others_sec=None,
    )
    return [synthetic, *sessions]


def auth_text(status: AuthStatus | None) -> str:
    """ステータスバーの AWS 表示（純粋）。profile と region は秘密ではない（BR-H2-AUTH-04）。"""
    if status is None:
        return "AWS 確認中…"
    if status.state is AuthState.ERROR:
        return "AWS 確認失敗"
    where = "/".join(part for part in (status.profile, status.region) if part) or "(不明)"
    return f"AWS {where} {'✓' if status.ok else f'✗ {status.label}'}"


def pipeline_states(stage: Stage | None, *, recording: bool, shared: bool) -> list[str]:
    """各段の表示状態（done / active / todo）を返す（純粋）。

    録音中は録音段だけを active にする（他は段検知より「今まさに録っている」を優先して見せる）。
    「共有」段だけは台帳ではなく、TUI 実行中に投稿が成功した記憶（`shared`）で完了を判定する。
    """
    if recording:
        return ["active", "todo", "todo", "todo"]
    if stage is None:
        return ["todo"] * len(STEP_KEYS)

    active_index = _STAGE_NEXT_STEP_INDEX.get(stage, 0)
    states: list[str] = []
    for i in range(len(STEP_KEYS)):
        if i < active_index:
            states.append("done")
        elif i == active_index:
            states.append("done" if (i == _SHARE_STEP_INDEX and shared) else "active")
        else:
            states.append("todo")
    return states


def pipeline_meta(
    states: list[str],
    stage: Stage | None,
    *,
    recording: bool,
    imported: bool = False,
    activity: Activity | None = None,
) -> list[str]:
    """各段の注記を返す（純粋）。段ごとに自分の状態を述べる。

    完了した段には完了の事実（「録音済」）が残り、対象段には次に必要なこと（「未共有」）が出る。
    以前は対象段にだけ `STAGE_LABELS` を載せていたが、その文言は完了を述べる文なので
    `minutes_done` のとき「04 共有＝議事録生成済」と読める表示になっていた。

    `imported` は取込（VTT/mp4）由来のセッション。録音が無いので 01 の完了注記を差し替える。
    `activity` が選択中セッションの進行中処理を指す場合、対象段に「（実行中）」を添える。
    """
    if recording:
        return ["録音中", _TODO_META, _TODO_META, _TODO_META]

    running = activity is not None  # ループ不変式（段ごとに評価しない）
    return [
        _step_meta(index, state, stage, imported=imported, running=running)
        for index, state in enumerate(states)
    ]


def _step_meta(index: int, state: str, stage: Stage | None, *, imported: bool, running: bool) -> str:
    """1 段ぶんの注記（純粋）。

    完了なら完了の事実、対象なら次に必要なこと、未着手なら `-` を返す。
    """
    if state == "done":
        return _IMPORTED_RECORD_META if (index == 0 and imported) else _DONE_METAS[index]
    if state == "active" and stage is not None:
        text = _ACTIVE_METAS[stage]
        return f"{text}（実行中）" if running else text
    return _TODO_META


class MeetingCommands(Provider):
    """コマンドパレット拡張。ボタンを廃止した Claude 経路をここから呼ぶ（他は既定コマンドのまま）。"""

    def _callback(self, key: str) -> IgnoreReturnCallbackType:
        # 実行時に import すると循環参照になるため cast で型だけ与える（App の実体は常に MeetingApp）。
        app = cast("MeetingApp", self.app)
        if key == "minutes_claude":
            return lambda: app._start_minutes(claude=True)
        return lambda: app._open_import_vtt_modal(claude=True)

    async def discover(self) -> Hits:
        for label, key, help_text in _PALETTE_COMMANDS:
            yield DiscoveryHit(label, self._callback(key), help=help_text)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for label, key, help_text in _PALETTE_COMMANDS:
            score = matcher.match(label)
            if score > 0:
                yield Hit(score, matcher.highlight(label), self._callback(key), help=help_text)


class Step(Vertical):
    """パイプライン段（#pipeline）の1コマ。set_state() で -active/-done を切り替える。"""

    def __init__(self, key: str, index: str, label: str) -> None:
        super().__init__(id=f"step-{key}", classes="step")
        self._index = index
        self._label = label

    def compose(self) -> ComposeResult:
        yield Static(f"{self._index} {self._label}", classes="step-title")
        yield Static("", classes="step-meta")

    def set_state(self, state: str, meta: str) -> None:
        self.set_class(state == "active", "-active")
        self.set_class(state == "done", "-done")
        self.query_one(".step-meta", Static).update(meta)


class SessionTable(DataTable[str]):
    """セッション一覧（#table）。最上位行で ↑ を押すと録音時間入力欄へフォーカスを移す。"""

    def on_key(self, event: events.Key) -> None:
        if event.key == "up" and self.cursor_row <= 0:
            event.stop()
            self.app.query_one("#record-minutes", Input).focus()


class RecordMinutesInput(Input):
    """録音時間の入力欄（#record-minutes）。↓ を押すとセッション一覧へフォーカスを移す。"""

    def on_key(self, event: events.Key) -> None:
        if event.key == "down":
            event.stop()
            self.app.query_one("#table", DataTable).focus()


class Logo(Static):
    """タイトルバー（Header の置き換え）。subtext の ASCII ロゴ＋ワードマーク。

    マイク部分（▟█▙ / ▜█▛ / ┴）のみ録音色（MIC_RED）にし、他は既定色のまま
    （Logo Marks 仕様: 赤は録音状態を示す役割色として意図的に拡張したもの）。
    """

    def __init__(self, cwd: Path) -> None:
        ver = _pkg_version("subtext-meeting")
        lines = (
            "  ┌───────┐",
            f"  │ ─────  [{MIC_RED}]▟█▙[/]    s u b t e x t  $ver",
            f"  │ ───    [{MIC_RED}]▜█▛[/]    record · minutes · slack",
            f"  │ ────    [{MIC_RED}]┴[/]    $cwd",
            "  └───────┘",
        )
        # マイク色にマークアップを使うため markup=False にはできない。外部由来のパス・バージョンは
        # `$変数` の置換で流し込む（置換値は再パースされないため、パスの ASCII 角括弧が消えない）。
        super().__init__(Content.from_markup("\n".join(lines), cwd=str(cwd), ver=ver), id="logo")
