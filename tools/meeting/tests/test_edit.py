"""議事録の手編集（edit.py）のテスト（②議事録編集）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meeting import edit


def _write_minutes(tmp_path: Path, markdown: str) -> Path:
    path = tmp_path / "minutes.md"
    path.write_text(markdown, encoding="utf-8")
    return path


_MARKDOWN = "# 議事録\n## 決定事項\n- 承認\n\n## ToDo\n\n## 論点・議論サマリ\n"


def test_resolve_editor_prefers_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDITOR", "code --wait")
    assert edit.resolve_editor() == "code --wait"


def test_resolve_editor_falls_back_to_notepad(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EDITOR", raising=False)
    assert edit.resolve_editor() == "notepad"


def test_resolve_editor_treats_blank_env_var_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDITOR", "   ")
    assert edit.resolve_editor() == "notepad"


def test_mark_edited_backs_up_and_sets_flag(tmp_path: Path) -> None:
    minutes_path = _write_minutes(tmp_path, _MARKDOWN)

    edit.mark_edited(minutes_path)

    assert edit.generated_path(minutes_path).read_text(encoding="utf-8") == _MARKDOWN
    assert edit.is_edited(minutes_path) is True


def test_mark_edited_does_not_overwrite_existing_backup(tmp_path: Path) -> None:
    minutes_path = _write_minutes(tmp_path, _MARKDOWN)
    edit.mark_edited(minutes_path)
    # 2回目の編集で本文を変える。
    minutes_path.write_text("# 議事録（編集後）\n", encoding="utf-8")

    edit.mark_edited(minutes_path)

    # 退避は最初の生成直後の版のまま（2回目の編集内容で上書きされない）。
    assert edit.generated_path(minutes_path).read_text(encoding="utf-8") == _MARKDOWN


def test_mark_edited_preserves_other_meta_keys(tmp_path: Path) -> None:
    minutes_path = _write_minutes(tmp_path, _MARKDOWN)
    edit.meta_path(minutes_path).write_text(
        json.dumps({"sessionId": "s1", "sourceModel": "fake"}), encoding="utf-8"
    )

    edit.mark_edited(minutes_path)

    data = json.loads(edit.meta_path(minutes_path).read_text(encoding="utf-8"))
    assert data["sessionId"] == "s1"
    assert data["sourceModel"] == "fake"
    assert data["edited"] is True


def test_mark_edited_survives_broken_existing_meta(tmp_path: Path) -> None:
    minutes_path = _write_minutes(tmp_path, _MARKDOWN)
    edit.meta_path(minutes_path).write_text("{not json", encoding="utf-8")

    edit.mark_edited(minutes_path)  # 例外を出さない

    assert edit.is_edited(minutes_path) is True


def test_is_edited_false_when_meta_missing(tmp_path: Path) -> None:
    minutes_path = _write_minutes(tmp_path, _MARKDOWN)
    assert edit.is_edited(minutes_path) is False


def test_is_edited_false_when_meta_broken(tmp_path: Path) -> None:
    minutes_path = _write_minutes(tmp_path, _MARKDOWN)
    edit.meta_path(minutes_path).write_text("{not json", encoding="utf-8")
    assert edit.is_edited(minutes_path) is False


def test_missing_headings_none_when_both_present() -> None:
    assert edit.missing_headings(_MARKDOWN) == ()


def test_missing_headings_flags_removed_decision_heading() -> None:
    markdown = "# 議事録\n## ToDo\n"
    assert edit.missing_headings(markdown) == ("## 決定事項",)


def test_missing_headings_flags_both_when_absent() -> None:
    markdown = "本文だけ\n"
    assert edit.missing_headings(markdown) == ("## 決定事項", "## ToDo")


def test_edit_raises_when_minutes_missing(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="議事録がありません"):
        edit.edit(tmp_path / "minutes.md")


def test_edit_marks_edited_before_launching_editor(tmp_path: Path) -> None:
    """退避は編集前の内容を捉える（エディタが呼ばれる前に mark_edited が走る）。"""
    minutes_path = _write_minutes(tmp_path, _MARKDOWN)
    launched: list[Path] = []

    def fake_launch(path: Path) -> None:
        launched.append(path)
        assert edit.generated_path(path).read_text(encoding="utf-8") == _MARKDOWN  # 退避済み
        path.write_text("# 議事録（編集後）\n## 決定事項\n- 追加\n## ToDo\n", encoding="utf-8")

    markdown, missing = edit.edit(minutes_path, launch=fake_launch)

    assert launched == [minutes_path]
    assert "編集後" in markdown
    assert missing == ()
    assert edit.is_edited(minutes_path) is True


def test_edit_reports_missing_headings_after_editor_removes_them(tmp_path: Path) -> None:
    minutes_path = _write_minutes(tmp_path, _MARKDOWN)

    def fake_launch(path: Path) -> None:
        path.write_text("本文だけになった\n", encoding="utf-8")

    _markdown, missing = edit.edit(minutes_path, launch=fake_launch)

    assert missing == ("## 決定事項", "## ToDo")


def test_default_launcher_invokes_subprocess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, check):
        calls.append(cmd)

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr("meeting.edit.subprocess.run", fake_run)
    launcher = edit.default_launcher("notepad")
    path = tmp_path / "minutes.md"

    launcher(path)

    assert calls == [["notepad", str(path)]]
