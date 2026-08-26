"""会議情報（MeetingInfo）の JSON 変換（BR-MI-01）。"""

from __future__ import annotations

from subtext_postmeeting.models import MeetingInfo


def test_from_json_empty_dict_is_empty() -> None:
    info = MeetingInfo.from_json({})

    assert info.is_empty()
    assert info.title is None
    assert info.meeting_datetime is None
    assert info.participants == ()


def test_from_json_reads_all_fields() -> None:
    info = MeetingInfo.from_json(
        {"title": "定例会", "datetime": "2026-08-26 10:00", "participants": ["田中", "山田（A社）"]}
    )

    assert info.is_empty() is False
    assert info.title == "定例会"
    assert info.meeting_datetime == "2026-08-26 10:00"
    assert info.participants == ("田中", "山田（A社）")


def test_from_json_blank_strings_are_treated_as_unset() -> None:
    """空白のみの記入は「記入なし」として扱う（未記入とタイプミスの空白を区別しない）。"""
    info = MeetingInfo.from_json({"title": "   ", "datetime": "", "participants": ["", "  ", "田中"]})

    assert info.title is None
    assert info.meeting_datetime is None
    assert info.participants == ("田中",)
    assert info.is_empty() is False


def test_to_json_round_trip() -> None:
    original = MeetingInfo(title="定例会", meeting_datetime="2026-08-26 10:00", participants=("田中",))

    restored = MeetingInfo.from_json(original.to_json())

    assert restored == original


def test_default_instance_is_empty() -> None:
    assert MeetingInfo().is_empty()
