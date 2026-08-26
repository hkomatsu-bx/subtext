"""会議情報（meeting_info.py）のテスト（FR-MI-01）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from meeting import meeting_info


def test_load_missing_file_returns_blank_template(tmp_path: Path) -> None:
    data = meeting_info.load(meeting_info.path_for(tmp_path))

    assert data == {"title": "", "datetime": "", "participants": []}


def test_load_raises_actionable_error_on_broken_json(tmp_path: Path) -> None:
    path = tmp_path / "meeting_info.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError, match="meeting_info.json"):
        meeting_info.load(path)


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "meeting_info.json"
    data = {"title": "定例会", "datetime": "2026-08-26 10:00", "participants": ["田中", "山田（A社）"]}

    meeting_info.save(path, data)

    assert meeting_info.load(path) == data


def test_is_empty_true_for_blank_fields() -> None:
    assert meeting_info.is_empty({"title": "", "datetime": "  ", "participants": []})
    assert meeting_info.is_empty({}) is True


def test_is_empty_false_when_any_field_filled() -> None:
    assert meeting_info.is_empty({"title": "定例会", "datetime": "", "participants": []}) is False
    assert meeting_info.is_empty({"title": "", "datetime": "", "participants": ["田中"]}) is False


def test_build_splits_participants_on_comma_and_touten() -> None:
    data = meeting_info.build("定例会", "2026-08-26 10:00", "田中、佐藤, 山田（A社）")

    assert data == {
        "title": "定例会",
        "datetime": "2026-08-26 10:00",
        "participants": ["田中", "佐藤", "山田（A社）"],
    }


def test_build_all_blank_yields_empty_template() -> None:
    data = meeting_info.build("", "", "")

    assert meeting_info.is_empty(data)


def test_build_strips_surrounding_whitespace() -> None:
    data = meeting_info.build("  定例会  ", "  2026-08-26 10:00  ", " 田中 , 佐藤 ")

    assert data["title"] == "定例会"
    assert data["datetime"] == "2026-08-26 10:00"
    assert data["participants"] == ["田中", "佐藤"]


def test_unmatched_participants_flags_resolved_speakers_missing_from_list() -> None:
    data = {"participants": ["田中"]}

    missing = meeting_info.unmatched_participants(data, ("田中", "佐藤"))

    assert missing == ("佐藤",)


def test_unmatched_participants_does_not_flag_unheard_participants() -> None:
    """参加者リストにあるが発言していない人は検出しない（発言しなかった参加者の記録が目的）。"""
    data = {"participants": ["田中", "山田（A社）"]}

    missing = meeting_info.unmatched_participants(data, ("田中",))

    assert missing == ()


def test_unmatched_participants_empty_list_flags_all_resolved_speakers() -> None:
    missing = meeting_info.unmatched_participants({}, ("田中", "self"))

    assert missing == ("田中", "self")
