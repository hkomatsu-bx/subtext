"""議事録の手編集（②議事録編集・Phase 1：外部エディタ）。

`minutes.md` を正本のまま外部エディタで開く。初回編集時に生成直後の版を `minutes.generated.md`
へ退避し、`minutes.meta.json` に `edited: true` を記録する。Unit B の
`PostMeetingPipeline._ensure_summarized` はこのフラグを見て、`--force` の明示指定なしには
`minutes.md` を上書きしない（手編集の消失を防ぐ）。

Slack 親メッセージ（`slack.build_parent`）は `minutes.md` の `## 決定事項` / `## ToDo` 見出しから
決定論的に抽出する（LLM 不使用）。編集でこの見出し文言を変えると、親メッセージは先頭本文の
フォールバックへ静かに落ちる（構造化サマリが消える）。保存後にこの検査を行い、欠けていれば
警告する（検査のみ・強制はしない。見出しを変えない運用は呼び出し側の運用規約に委ねる）。
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Callable

from meeting import slack

# エディタ起動の seam（テストは実プロセスを起動しない）。
Launcher = Callable[[Path], None]

_DECISION_KEYWORD = "決定事項"
_TODO_KEYWORD = "ToDo"


def resolve_editor() -> str:
    """`$EDITOR` → `notepad`（Windows 既定・未設定時のフォールバック）。"""
    return os.environ.get("EDITOR", "").strip() or "notepad"


def split_editor_command(editor: str) -> list[str]:
    """`$EDITOR` を実行ファイルと引数へ分解する（純粋）。

    `EDITOR="code --wait"` のように引数付きの指定が一般的で、文字列をそのまま実行ファイル名として
    渡すと「`code --wait` という名前の実行ファイル」を探して FileNotFoundError になる。
    `posix=False` にするのは Windows のパス（`C:\\Program Files\\...`）のバックスラッシュを
    エスケープとして食べさせないためで、引用符で囲んだパスもそのまま扱える。
    """
    parts = shlex.split(editor, posix=False)
    # posix=False は引用符を残すため、実行ファイル名として使う前に落とす。
    return [part.strip('"') for part in parts if part]


def default_launcher(editor: str) -> Launcher:
    """既定のランチャ。エディタが閉じるまで待つ（`subprocess.run` はブロッキング）。"""
    command = split_editor_command(editor)

    def _launch(path: Path) -> None:
        subprocess.run([*command, str(path)], check=False)

    return _launch


def generated_path(minutes_path: Path) -> Path:
    return minutes_path.with_name("minutes.generated.md")


def meta_path(minutes_path: Path) -> Path:
    return minutes_path.with_name("minutes.meta.json")


def is_edited(minutes_path: Path) -> bool:
    """`minutes.meta.json` の `edited` フラグを読む（未存在・壊れていれば False）。"""
    path = meta_path(minutes_path)
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(data.get("edited", False))


def mark_edited(minutes_path: Path) -> None:
    """初回編集を記録する: 生成直後の版を退避し `edited: true` を立てる（非破壊・冪等）。

    2回目以降の呼び出しでは退避を上書きしない（`minutes.generated.md` は常に「最初の
    生成直後の版」を保つ。編集を重ねるたびに退避が更新されると、比較材料としての意味が薄れる）。
    """
    gen_path = generated_path(minutes_path)
    if not gen_path.is_file() and minutes_path.is_file():
        gen_path.write_text(minutes_path.read_text(encoding="utf-8"), encoding="utf-8")

    meta = meta_path(minutes_path)
    data: dict = {}
    if meta.is_file():
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    data["edited"] = True
    meta.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def missing_headings(markdown: str) -> tuple[str, ...]:
    """Slack 親メッセージの抽出に要る見出しが欠けていれば列挙する（純粋）。"""
    missing = []
    if not slack.has_section(markdown, _DECISION_KEYWORD):
        missing.append(f"## {_DECISION_KEYWORD}")
    if not slack.has_section(markdown, _TODO_KEYWORD):
        missing.append(f"## {_TODO_KEYWORD}")
    return tuple(missing)


def edit(minutes_path: Path, *, launch: Launcher | None = None) -> tuple[str, tuple[str, ...]]:
    """議事録を編集する。存在しなければ actionable エラー。

    戻り値は (保存後の本文, 欠落見出しのタプル)。案内文の出力は呼び出し側（CLI/TUI）に委ねる。
    """
    if not minutes_path.is_file():
        raise ValueError(f"議事録がありません（先に議事録を生成してください）: {minutes_path}")
    mark_edited(minutes_path)  # エディタを開く前に「生成直後の版」を退避する
    launcher = launch or default_launcher(resolve_editor())
    launcher(minutes_path)
    markdown = minutes_path.read_text(encoding="utf-8")
    return markdown, missing_headings(markdown)
