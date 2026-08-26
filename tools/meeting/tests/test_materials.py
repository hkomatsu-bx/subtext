"""付帯資料の投入（materials.py）のテスト（③付帯資料）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from meeting import materials
from meeting.config import MeetingConfig


def _touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_input_lines_strips_and_drops_blank_lines() -> None:
    raw = "  a.txt  \n\n   \nb.pdf\n"
    assert materials.parse_input_lines(raw) == ("a.txt", "b.pdf")


def test_resolve_paths_single_file(tmp_path: Path) -> None:
    f = _touch(tmp_path / "a.txt")
    resolved, warnings = materials.resolve_paths((str(f),))
    assert resolved == (f,)
    assert warnings == ()


def test_resolve_paths_expands_directory_supported_only(tmp_path: Path) -> None:
    d = tmp_path / "docs"
    a = _touch(d / "a.txt")
    _touch(d / "b.png")  # 未対応拡張子
    resolved, warnings = materials.resolve_paths((str(d),))
    assert resolved == (a,)
    assert any("b.png" in w for w in warnings)


def test_resolve_paths_expands_glob(tmp_path: Path) -> None:
    a = _touch(tmp_path / "a.pptx")
    b = _touch(tmp_path / "b.pptx")
    resolved, warnings = materials.resolve_paths((str(tmp_path / "*.pptx"),))
    assert set(resolved) == {a, b}
    assert warnings == ()


def test_resolve_paths_warns_on_no_match(tmp_path: Path) -> None:
    resolved, warnings = materials.resolve_paths((str(tmp_path / "nope.txt"),))
    assert resolved == ()
    assert "該当するファイルがありません" in warnings[0]


def test_resolve_paths_warns_on_duplicate_filenames(tmp_path: Path) -> None:
    a = _touch(tmp_path / "one" / "a.txt")
    _touch(tmp_path / "two" / "a.txt")
    resolved, warnings = materials.resolve_paths((str(a), str(tmp_path / "two" / "a.txt")))
    assert resolved == (a,)
    assert any("重複" in w for w in warnings)


def test_resolve_paths_combines_multiple_lines(tmp_path: Path) -> None:
    a = _touch(tmp_path / "a.txt")
    b = _touch(tmp_path / "b.md")
    resolved, warnings = materials.resolve_paths((str(a), str(b)))
    assert resolved == (a, b)
    assert warnings == ()


def test_copy_into_places_files_in_materials_dir(cfg: MeetingConfig, tmp_path: Path) -> None:
    a = _touch(tmp_path / "src" / "a.txt", "本文")
    materials_dir = materials.copy_into(cfg, "20260625-120156", (a,))
    assert (materials_dir / "a.txt").read_text(encoding="utf-8") == "本文"


def test_copy_into_overwrites_existing_same_name(cfg: MeetingConfig, tmp_path: Path) -> None:
    a = _touch(tmp_path / "src" / "a.txt", "新しい本文")
    materials_dir = cfg.session_out_dir("20260625-120156") / "materials"
    materials_dir.mkdir(parents=True)
    (materials_dir / "a.txt").write_text("古い本文", encoding="utf-8")

    materials.copy_into(cfg, "20260625-120156", (a,))

    assert (materials_dir / "a.txt").read_text(encoding="utf-8") == "新しい本文"
