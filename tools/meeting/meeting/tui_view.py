"""TUI の表示部品と、画面へ流す値の純粋計算。

`tui.py` から切り出した「見せ方」の層。段の状態（完了/対象/未着手）とその注記は App の状態に
触らない純粋関数にしてあるため、Pilot を起動せずに単体テストできる。

外部由来の文字列（セッションID・パス）を出す箇所のマークアップ扱いは `tui_modals` の docstring
と同じ制約に従う。
"""

from __future__ import annotations

from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import TYPE_CHECKING, cast

from textual import events
from textual.app import ComposeResult
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Vertical
from textual.content import Content
from textual.types import IgnoreReturnCallbackType
from textual.widgets import DataTable, Input, Static

from meeting.runner import Stage

if TYPE_CHECKING:  # 実行時 import は循環参照になるため型検査時のみ。
    from meeting.tui import MeetingApp

# セッション一覧（#table）の列。
COLUMNS = ("#", "セッション", "段", "録音状態", "self(s)", "others(s)")
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


def pipeline_meta(states: list[str], stage: Stage | None, *, recording: bool, stage_label: str = "") -> list[str]:
    """各段の注記を返す（純粋）。active な段にだけ現在段のラベルを載せる。"""
    meta = ["-"] * len(STEP_KEYS)
    if recording:
        meta[0] = "録音中"
        return meta
    active_index = next((i for i, s in enumerate(states) if s == "active"), None)
    if active_index is not None and stage is not None:
        meta[active_index] = stage_label
    return meta


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
