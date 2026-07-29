"""tui_view.py の純粋部の単体テスト（Pilot を起動しない）。

パイプライン段の表示状態は元は App のメソッドで、`_is_recording()` や `_shared_sessions` を
直接参照していたため Pilot 経由でしか検証できなかった。純粋関数へ切り出した結果、
組み合わせを直接固定できる。
"""

from __future__ import annotations

import pytest

from meeting.runner import Stage
from meeting.tui_view import (
    STEP_INDEXES,
    STEP_KEYS,
    STEP_LABELS,
    format_seconds,
    pipeline_meta,
    pipeline_states,
    strip_path_quotes,
)


@pytest.mark.unit
def test_step_tables_have_matching_lengths() -> None:
    """key / ラベル / 通し番号は zip(strict=True) で束ねるため長さが揃っていること。"""
    assert len(STEP_KEYS) == len(STEP_LABELS) == len(STEP_INDEXES)


@pytest.mark.unit
def test_pipeline_states_without_session_is_all_todo() -> None:
    assert pipeline_states(None, recording=False, shared=False) == ["todo"] * len(STEP_KEYS)


@pytest.mark.unit
def test_pipeline_states_while_recording_highlights_only_record_step() -> None:
    """録音中は段検知より「今まさに録っている」を優先して見せる。"""
    states = pipeline_states(Stage.MINUTES_DONE, recording=True, shared=True)

    assert states == ["active", "todo", "todo", "todo"]


@pytest.mark.unit
def test_pipeline_states_marks_earlier_steps_done() -> None:
    states = pipeline_states(Stage.TRANSCRIPT_DONE, recording=False, shared=False)

    assert states == ["done", "done", "active", "todo"]


@pytest.mark.unit
def test_pipeline_states_naming_required_is_still_on_import_step() -> None:
    """話者名の記入待ちは「取込」段に留まる（議事録段へは進めていない）。"""
    assert pipeline_states(Stage.NAMING_REQUIRED, recording=False, shared=False)[1] == "active"


@pytest.mark.unit
def test_pipeline_states_share_step_completes_only_when_shared() -> None:
    """共有段は台帳ではなく「TUI 実行中に投稿が成功した記憶」で完了になる。"""
    assert pipeline_states(Stage.MINUTES_DONE, recording=False, shared=False)[3] == "active"
    assert pipeline_states(Stage.MINUTES_DONE, recording=False, shared=True)[3] == "done"


@pytest.mark.unit
def test_pipeline_meta_labels_only_active_step() -> None:
    states = pipeline_states(Stage.TRANSCRIPT_DONE, recording=False, shared=False)

    meta = pipeline_meta(states, Stage.TRANSCRIPT_DONE, recording=False, stage_label="トランスクリプト完了")

    assert meta == ["-", "-", "トランスクリプト完了", "-"]


@pytest.mark.unit
def test_pipeline_meta_while_recording() -> None:
    states = pipeline_states(Stage.RECORDED, recording=True, shared=False)

    assert pipeline_meta(states, Stage.RECORDED, recording=True, stage_label="録音済") == ["録音中", "-", "-", "-"]


@pytest.mark.unit
def test_format_seconds_marks_missing_recording() -> None:
    assert format_seconds(120.44) == "120.4"
    assert format_seconds(None) == "-"  # 取込由来は録音そのものが無い


@pytest.mark.unit
def test_strip_path_quotes_only_removes_matching_pairs() -> None:
    assert strip_path_quotes('"C:\\path\\meeting.vtt"') == "C:\\path\\meeting.vtt"
    assert strip_path_quotes("'meeting.vtt'") == "meeting.vtt"
    assert strip_path_quotes("meeting.vtt") == "meeting.vtt"
    assert strip_path_quotes('"meeting.vtt') == '"meeting.vtt'
