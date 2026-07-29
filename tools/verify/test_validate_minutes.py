"""validate_minutes（SC-P3 議事録構造検証）の単体テスト。

純粋部（_split_sections / _resolve_minutes / _check）と validate の合否判定を検証する。
議事録本文は PII を含みうるため、テストは非機密のダミー文面のみを用いる。
実行: uv run pytest tools/verify/test_validate_minutes.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import validate_minutes as vm  # noqa: E402

_GOOD = """## 決定事項
- A社対応を承認。

## ToDo
- [ ] タスクX（担当: 自分 / 期限: 8/1）

## 論点・議論サマリ
- 論点1について議論した。
"""


# --- _split_sections -------------------------------------------------------
def test_split_sections_by_heading() -> None:
    markdown = "# 議事録\n## A\nbody a\n## B\nbody b1\nbody b2\n"

    sections = vm._split_sections(markdown)

    assert sections["A"] == "body a"
    assert sections["B"] == "body b1\nbody b2"
    assert "議事録" not in sections  # h1(#) は対象外（## のみ）


def test_split_sections_trailing_section() -> None:
    sections = vm._split_sections("## X\nline1\nline2\n")

    assert sections["X"] == "line1\nline2"


# --- _resolve_minutes / _check --------------------------------------------
def test_resolve_minutes_directory(tmp_path: Path) -> None:
    assert vm._resolve_minutes(tmp_path) == tmp_path / "minutes.md"


def test_check_marks_by_state() -> None:
    assert vm._check("x", True, "d")[0] == "OK "
    assert vm._check("x", None, "d")[0] == "▲ "
    assert vm._check("x", False, "d")[0] == "NG "


# --- validate --------------------------------------------------------------
def test_validate_pass_on_well_formed_minutes(tmp_path: Path, capsys) -> None:
    path = tmp_path / "minutes.md"
    path.write_text(_GOOD, encoding="utf-8")

    rc = vm.validate(path)

    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out


def test_validate_missing_file(tmp_path: Path, capsys) -> None:
    rc = vm.validate(tmp_path / "minutes.md")

    assert rc == 1
    assert "見つかりません" in capsys.readouterr().out


def test_validate_missing_required_section_fails(tmp_path: Path, capsys) -> None:
    # 「論点・議論サマリ」欠落 → NG → FAIL。
    path = tmp_path / "minutes.md"
    path.write_text("## 決定事項\n- A。\n\n## ToDo\n- [ ] x\n", encoding="utf-8")

    rc = vm.validate(path)

    assert rc == 1
    assert "FAIL" in capsys.readouterr().out


def test_validate_empty_section_fails(tmp_path: Path) -> None:
    # 見出しはあるが本文が空 → NG。
    markdown = "## 決定事項\n- A。\n\n## ToDo\n- [ ] x\n\n## 論点・議論サマリ\n"
    path = tmp_path / "minutes.md"
    path.write_text(markdown, encoding="utf-8")

    assert vm.validate(path) == 1


def test_validate_zero_todo_items_is_review_not_fail(tmp_path: Path, capsys) -> None:
    # ToDo が `- [ ]` を含まない（0件）は ▲（要確認）であって NG ではない。
    markdown = "## 決定事項\n- A。\n\n## ToDo\n該当なし。\n\n## 論点・議論サマリ\n- 論点。\n"
    path = tmp_path / "minutes.md"
    path.write_text(markdown, encoding="utf-8")

    rc = vm.validate(path)

    out = capsys.readouterr().out
    assert rc == 0
    assert "0件" in out


def test_validate_truncation_flagged_as_review(tmp_path: Path, capsys) -> None:
    # 末尾が文末記号でない＝截断の疑い → ▲（要確認）。必須3節は揃うため rc=0。
    markdown = "## 決定事項\n- A。\n\n## ToDo\n- [ ] x\n\n## 論点・議論サマリ\n- 途中で切れた文字"
    path = tmp_path / "minutes.md"
    path.write_text(markdown, encoding="utf-8")

    rc = vm.validate(path)

    out = capsys.readouterr().out
    assert rc == 0
    assert "截断の疑い" in out


# --- main ------------------------------------------------------------------
def test_main_requires_single_arg(capsys) -> None:
    assert vm.main([]) == 2
