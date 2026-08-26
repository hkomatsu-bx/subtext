"""TUI のモーダル画面（確認・パス入力・Slack 投稿プレビュー・話者名記入）。

`tui.py` から切り出した表示専用の部品。ロジックは持たず、結果は `dismiss` で呼び出し側へ返す。

**外部由来の文字列を表示する Static は `markup=False` にする**。議事録本文・ファイルパス・
セッションID・話者名は `[...]` を含み得る。既定（markup=True）では Textual がこれをスタイルタグ
として解釈し、`[ap-northeast-1]` のような角括弧は**黙って消える**。承認画面で消えた文字列は
実際の投稿本文には残るため、「見たものと送るものが違う」状態になり承認の意味が崩れる。
`[/]` を含む場合は MarkupError で画面の生成自体が失敗する。
"""

from __future__ import annotations

from typing import Sequence

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static, TextArea

from meeting import naming


class ConfirmModal(ModalScreen[bool]):
    """Yes/No 確認モーダル（取り消せない操作の人間承認用）。

    **承認ボタンの文言は呼び出し側が `confirm_label` で必ず渡す**。文言を既定値で固定していた
    ため、Slack 投稿の承認からセッション削除へ転用したときに「投稿する (y)」のまま残り、不可逆な
    削除（録音 WAV の唯一のコピーを含む rmtree）の唯一のゲートが投稿ボタンに見えていた。
    Slack 投稿を繰り返した操作者が反射的に y を押すとセッションが失われる。

    本文（プレビュー）は長くなり得るため内部だけスクロールさせ、Yes/No ボタンは常に固定表示
    する（ボタンをスクロール領域の中に置くと、長い議事録でボタンが画面外へ押し出され
    クリックできなくなるため）。

    **本文を表示する Static は `markup=False` にする**。本文は議事録やファイルパスなど外部由来の
    文字列で、`[...]` を含み得る。既定（markup=True）では Textual がこれをスタイルタグとして
    解釈し、`[ap-northeast-1]` のような角括弧は**黙って消える**。承認画面で消えた文字列は
    投稿本文には残るため、「見たものと送るものが違う」状態になり承認の意味が崩れる。
    `[/]` を含む場合は MarkupError で画面の生成自体が失敗する。
    """

    DEFAULT_CSS = """
    ConfirmModal {
        align: center middle;
    }
    ConfirmModal > Vertical {
        width: 90%;
        height: 80%;
        border: round $accent;
        padding: 1 2;
        background: $surface;
    }
    ConfirmModal .modal-title {
        text-style: bold;
        padding-bottom: 1;
    }
    ConfirmModal VerticalScroll {
        height: 1fr;
    }
    ConfirmModal Horizontal {
        height: auto;
        padding-top: 1;
    }
    """
    BINDINGS = [("y", "confirm", ""), ("n", "cancel", ""), ("escape", "cancel", "")]

    def __init__(self, title: str, body: str, *, confirm_label: str) -> None:
        super().__init__()
        self._title = title
        self._body = body
        self._confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._title, classes="modal-title")
            with VerticalScroll():
                # 本文はファイルパス等の外部由来文字列を含む。markup=False は必須（クラス docstring 参照）。
                yield Static(self._body, id="confirm-body", markup=False)
            with Horizontal():
                yield Button(self._confirm_label, id="yes", classes="dialog-btn-primary")
                yield Button("中止 (N)", id="no", classes="dialog-btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class PathInputModal(ModalScreen[str | None]):
    """パス入力モーダル（VTTインポート）。OKで文字列（空も含む）、キャンセルで None を返す。"""

    DEFAULT_CSS = """
    PathInputModal {
        align: center middle;
    }
    PathInputModal > Vertical {
        width: 80%;
        height: auto;
        border: round $accent;
        padding: 1 2;
        background: $surface;
    }
    PathInputModal .modal-title {
        text-style: bold;
        padding-bottom: 1;
    }
    PathInputModal Horizontal {
        height: auto;
        padding-top: 1;
    }
    """
    BINDINGS = [("escape", "cancel", "")]

    def __init__(self, title: str) -> None:
        super().__init__()
        self._title = title

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._title, classes="modal-title")
            yield Input(id="path-input", placeholder="VTTファイルパス")
            with Horizontal():
                yield Button("取り込む", id="ok", classes="dialog-btn-primary")
                yield Button("キャンセル (Esc)", id="cancel", classes="dialog-btn-cancel")

    def on_mount(self) -> None:
        self.query_one("#path-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self.dismiss(self.query_one("#path-input", Input).value.strip())
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss(None)


class SlackPostModal(ModalScreen[str | None]):
    """Slack投稿プレビュー＋投稿先チャンネル入力を1画面に統合したモーダル。

    OKで入力欄の文字列（空も含む）、キャンセルで None を返す。

    プレビューの Static は `markup=False` にする（理由は ConfirmModal の docstring を参照。
    議事録本文の `[...]` が黙って消え、承認したプレビューと投稿本文が食い違うのを防ぐ）。
    """

    DEFAULT_CSS = """
    SlackPostModal {
        align: center middle;
    }
    SlackPostModal > Vertical {
        width: 90%;
        height: 80%;
        border: round $accent;
        padding: 1 2;
        background: $surface;
    }
    SlackPostModal .modal-title {
        text-style: bold;
        padding-bottom: 1;
    }
    SlackPostModal VerticalScroll {
        height: 1fr;
    }
    SlackPostModal .field-label {
        padding-top: 1;
    }
    SlackPostModal Horizontal {
        height: auto;
        padding-top: 1;
    }
    """
    BINDINGS = [("escape", "cancel", "")]

    def __init__(self, title: str, preview: str, default_channel: str) -> None:
        super().__init__()
        self._title = title
        self._preview = preview
        self._default_channel = default_channel

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._title, classes="modal-title")
            with VerticalScroll():
                yield Static(self._preview, id="slack-preview", markup=False)
            yield Static("投稿先チャンネルID", classes="field-label")
            yield Input(id="channel-input", value=self._default_channel, placeholder="Slackチャンネル")
            with Horizontal():
                yield Button("投稿する", id="ok", classes="dialog-btn-primary")
                yield Button("中止 (Esc)", id="cancel", classes="dialog-btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self.dismiss(self.query_one("#channel-input", Input).value.strip())
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SpeakerNamesModal(ModalScreen[dict[str, str] | None]):
    """話者名の記入モーダル（BR-NAME-01/02・FR-H2-03）。

    ラベルごとに発話数と代表発言を提示し、実名を入力させる。OK で `{ラベル: 入力値}`、キャンセルで
    None を返す（空入力のラベルは呼び出し側が naming.apply_names でスキップする）。

    実名と代表発言（PII）はこの画面にのみ表示し、ログへは書かない（BR-NAME-04）。ラベル・発言は
    `[` を含みうるため、表示する Static はすべて `markup=False` にする（Textual のマークアップ
    として解釈されると本文が消えたり例外になる）。
    """

    DEFAULT_CSS = """
    SpeakerNamesModal {
        align: center middle;
    }
    SpeakerNamesModal > Vertical {
        width: 90%;
        height: 80%;
        border: round $accent;
        padding: 1 2;
        background: $surface;
    }
    SpeakerNamesModal .modal-title {
        text-style: bold;
        padding-bottom: 1;
    }
    SpeakerNamesModal VerticalScroll {
        height: 1fr;
    }
    SpeakerNamesModal .speaker-label {
        text-style: bold;
        padding-top: 1;
    }
    SpeakerNamesModal .speaker-sample {
        color: $text-muted;
    }
    SpeakerNamesModal Horizontal {
        height: auto;
        padding-top: 1;
    }
    """
    BINDINGS = [("escape", "cancel", "")]

    def __init__(self, title: str, prompts: Sequence[naming.SpeakerPrompt]) -> None:
        super().__init__()
        self._title = title
        self._prompts = tuple(prompts)

    def compose(self) -> ComposeResult:
        with Vertical():
            # タイトルはセッションID（VTT/mp4 のファイル名由来）を含むため markup=False。
            yield Static(self._title, classes="modal-title", markup=False)
            yield Static(
                "初期値は VTT/ライブ字幕由来の候補です（「相手」「Speaker 1」等の仮名を含みます）。"
                "確認・補正してください。候補が無いラベルを空欄にすると spk_n 表記で議事録に残ります。",
                classes="speaker-sample",
                markup=False,
            )
            with VerticalScroll():
                for i, prompt in enumerate(self._prompts):
                    yield Static(
                        f"[{prompt.label}] 発話数 {prompt.segment_count}",
                        classes="speaker-label",
                        markup=False,
                    )
                    for sample in prompt.samples:
                        yield Static(f"    「{sample}」", classes="speaker-sample", markup=False)
                    # 候補（`current`）を初期値に入れる。空のままだと操作者が何を確定したのか
                    # 分からず、仮名がそのまま議事録の話者名になる（BR-NAME-01）。
                    yield Input(id=f"speaker-{i}", value=prompt.current, placeholder=f"{prompt.label} の実名")
            with Horizontal():
                yield Button("保存して続行", id="ok", classes="dialog-btn-primary")
                yield Button("中止 (Esc)", id="cancel", classes="dialog-btn-cancel")

    def on_mount(self) -> None:
        if self._prompts:
            self.query_one("#speaker-0", Input).focus()

    def _collect(self) -> dict[str, str]:
        return {p.label: self.query_one(f"#speaker-{i}", Input).value for i, p in enumerate(self._prompts)}

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self._collect() if event.button.id == "ok" else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class MeetingInfoModal(ModalScreen[dict[str, str] | None]):
    """会議情報（会議名・日時・参加者）の記入モーダル（FR-MI-01）。

    すべて任意。OK で `{"title": ..., "datetime": ..., "participants": "..."}`（参加者はカンマ／
    読点区切りの1行）を返し、キャンセルで None を返す。参加者名は PII を含み得るため、この画面
    以外（ログ等）へは出さない（BR-NAME-04 と同じ扱い）。既存値があれば初期値として表示する。
    """

    DEFAULT_CSS = """
    MeetingInfoModal {
        align: center middle;
    }
    MeetingInfoModal > Vertical {
        width: 70%;
        height: auto;
        border: round $accent;
        padding: 1 2;
        background: $surface;
    }
    MeetingInfoModal .modal-title {
        text-style: bold;
        padding-bottom: 1;
    }
    MeetingInfoModal .field-label {
        padding-top: 1;
    }
    MeetingInfoModal Horizontal {
        height: auto;
        padding-top: 1;
    }
    """
    BINDINGS = [("escape", "cancel", "")]

    def __init__(self, title: str, *, initial: dict[str, str] | None = None) -> None:
        super().__init__()
        self._title = title
        initial = initial or {}
        self._initial_title = str(initial.get("title") or "")
        self._initial_datetime = str(initial.get("datetime") or "")
        self._initial_participants = str(initial.get("participants") or "")

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._title, classes="modal-title", markup=False)
            yield Static("すべて任意です。空欄のままなら既存の算出値のまま議事録を生成します。", markup=False)
            yield Static("会議名", classes="field-label")
            yield Input(id="mi-title", value=self._initial_title, placeholder="例: 定例会")
            yield Static("日時", classes="field-label")
            yield Input(id="mi-datetime", value=self._initial_datetime, placeholder="例: 2026-08-26 10:00")
            yield Static("参加者（読点またはカンマ区切り）", classes="field-label")
            yield Input(id="mi-participants", value=self._initial_participants, placeholder="例: 田中、山田（A社）")
            with Horizontal():
                yield Button("保存して続行", id="ok", classes="dialog-btn-primary")
                yield Button("スキップ (Esc)", id="cancel", classes="dialog-btn-cancel")

    def on_mount(self) -> None:
        self.query_one("#mi-title", Input).focus()

    def _collect(self) -> dict[str, str]:
        return {
            "title": self.query_one("#mi-title", Input).value,
            "datetime": self.query_one("#mi-datetime", Input).value,
            "participants": self.query_one("#mi-participants", Input).value,
        }

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self._collect() if event.button.id == "ok" else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class MaterialsInputModal(ModalScreen[str | None]):
    """付帯資料の投入モーダル（③付帯資料）。複数行入力（1行1パス／フォルダ／glob）を受ける。

    OK で複数行文字列（空も含む）、Esc/キャンセルで None を返す。実際の解決・コピーは
    呼び出し側（`materials.resolve_paths`/`materials.copy_into`）に委ねる。
    """

    DEFAULT_CSS = """
    MaterialsInputModal {
        align: center middle;
    }
    MaterialsInputModal > Vertical {
        width: 80%;
        height: 70%;
        border: round $accent;
        padding: 1 2;
        background: $surface;
    }
    MaterialsInputModal .modal-title {
        text-style: bold;
        padding-bottom: 1;
    }
    MaterialsInputModal TextArea {
        height: 1fr;
    }
    MaterialsInputModal Horizontal {
        height: auto;
        padding-top: 1;
    }
    """
    BINDINGS = [("escape", "cancel", "")]

    def __init__(self, title: str) -> None:
        super().__init__()
        self._title = title

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._title, classes="modal-title")
            yield Static(
                "1行に1つ：ファイルパス／フォルダ／glob（*.pptx 等）。対応形式は .txt/.md/.pdf/.pptx。",
                markup=False,
            )
            yield TextArea(id="materials-input")
            with Horizontal():
                yield Button("投入する", id="ok", classes="dialog-btn-primary")
                yield Button("キャンセル (Esc)", id="cancel", classes="dialog-btn-cancel")

    def on_mount(self) -> None:
        self.query_one("#materials-input", TextArea).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self.dismiss(self.query_one("#materials-input", TextArea).text)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
