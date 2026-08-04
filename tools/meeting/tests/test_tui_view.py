"""tui_view.py の純粋部の単体テスト（Pilot を起動しない）。

パイプライン段の表示状態は元は App のメソッドで、`_is_recording()` や `_shared_sessions` を
直接参照していたため Pilot 経由でしか検証できなかった。純粋関数へ切り出した結果、
組み合わせを直接固定できる。
"""

from __future__ import annotations

import pytest

from textual.content import Content

from meeting.runner import STAGE_LABELS, AuthState, AuthStatus, SessionSummary, Stage
from meeting.tui_view import (
    ACTIVITY_ACCENT,
    MIC_RED,
    STEP_INDEXES,
    STEP_KEYS,
    STEP_LABELS,
    Activity,
    auth_text,
    column_widths,
    format_seconds,
    merge_recording_session,
    number_cell,
    pipeline_meta,
    pipeline_states,
    stage_cell,
    strip_path_quotes,
)


def _summary(session_id: str, stage: Stage, *, self_sec: float | None = 1.0, status: str = "complete") -> SessionSummary:
    return SessionSummary(
        session_id=session_id, stage=stage, status=status, self_sec=self_sec, others_sec=self_sec
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
def test_pipeline_meta_labels_each_step_by_its_own_state() -> None:
    """完了した段には完了の事実が残り、対象段には次に必要なことが出る。"""
    states = pipeline_states(Stage.TRANSCRIPT_DONE, recording=False, shared=False)

    meta = pipeline_meta(states, Stage.TRANSCRIPT_DONE, recording=False)

    assert meta == ["録音済", "文字起こし済", "議事録未生成", "-"]


@pytest.mark.unit
def test_pipeline_meta_does_not_put_done_label_on_share_step() -> None:
    """回帰防止: `minutes_done` で「議事録生成済」が 04 共有の注記に出ないこと。

    以前は「現在段のラベル（STAGE_LABELS）を対象段にだけ載せる」実装だったため、完了を述べる
    文言が未実施の共有段の下に出て、共有が済んだように読めていた。
    """
    states = pipeline_states(Stage.MINUTES_DONE, recording=False, shared=False)

    meta = pipeline_meta(states, Stage.MINUTES_DONE, recording=False)

    assert meta == ["録音済", "文字起こし済", "議事録生成済", "未共有"]
    assert meta[3] != STAGE_LABELS[Stage.MINUTES_DONE]


@pytest.mark.unit
def test_pipeline_meta_keeps_naming_gate_visible() -> None:
    """話者名の記入待ちは課金の停止点。対象段の注記として残す。"""
    states = pipeline_states(Stage.NAMING_REQUIRED, recording=False, shared=False)

    assert pipeline_meta(states, Stage.NAMING_REQUIRED, recording=False)[1] == "話者名の記入待ち"


@pytest.mark.unit
def test_pipeline_meta_imported_session_has_no_recording() -> None:
    """取込（VTT/mp4）由来は録音そのものが無いため「録音済」とは書かない。"""
    states = pipeline_states(Stage.TRANSCRIPT_DONE, recording=False, shared=False)

    assert pipeline_meta(states, Stage.TRANSCRIPT_DONE, recording=False, imported=True)[0] == "録音なし"


@pytest.mark.unit
def test_pipeline_meta_marks_active_step_as_running() -> None:
    states = pipeline_states(Stage.TRANSCRIPT_DONE, recording=False, shared=False)

    meta = pipeline_meta(states, Stage.TRANSCRIPT_DONE, recording=False, activity=Activity.MINUTES)

    assert meta[2] == "議事録未生成（実行中）"


@pytest.mark.unit
def test_pipeline_meta_shared_step_shows_done() -> None:
    states = pipeline_states(Stage.MINUTES_DONE, recording=False, shared=True)

    assert pipeline_meta(states, Stage.MINUTES_DONE, recording=False)[3] == "共有済"


@pytest.mark.unit
def test_pipeline_meta_while_recording() -> None:
    states = pipeline_states(Stage.RECORDED, recording=True, shared=False)

    assert pipeline_meta(states, Stage.RECORDED, recording=True) == ["録音中", "-", "-", "-"]


@pytest.mark.unit
def test_pipeline_meta_without_session_is_all_placeholder() -> None:
    assert pipeline_meta(["todo"] * 4, None, recording=False) == ["-"] * len(STEP_KEYS)


# --- 一覧の処理中マーカー ---------------------------------------------------------


@pytest.mark.unit
def test_number_cell_marks_recording_with_record_colour() -> None:
    cell = number_cell(3, Activity.RECORDING)

    assert cell.plain == "● 3"
    assert [span.style for span in cell.spans] == [MIC_RED]


@pytest.mark.unit
def test_number_cell_marks_other_work_with_accent() -> None:
    for activity in (Activity.MINUTES, Activity.IMPORT, Activity.SHARING):
        cell = number_cell(1, activity)

        assert cell.plain == "◐ 1"
        assert [span.style for span in cell.spans] == [ACTIVITY_ACCENT]


@pytest.mark.unit
def test_number_cell_blink_off_phase_keeps_colour_and_width() -> None:
    """消灯側は中抜きの丸にするだけ（色と幅は変えない＝脈打って見える）。"""
    lit = number_cell(3, Activity.RECORDING)
    off = number_cell(3, Activity.RECORDING, lit=False)

    assert off.plain == "○ 3"
    assert [span.style for span in off.spans] == [MIC_RED]  # 消灯でも色は残す
    assert len(off.plain) == len(lit.plain)


@pytest.mark.unit
def test_number_cell_blink_applies_to_other_work_too() -> None:
    assert number_cell(2, Activity.MINUTES, lit=False).plain == "○ 2"


@pytest.mark.unit
def test_number_cell_idle_row_does_not_blink() -> None:
    """待機行は位相に関わらず記号を出さない（点滅させるのは処理中だけ）。"""
    assert number_cell(1, None, lit=False).plain == number_cell(1, None).plain == "  1"


@pytest.mark.unit
def test_number_cell_idle_row_keeps_number_aligned() -> None:
    """待機行はマーカー幅を空白で埋める（桁がずれると列全体が動いて読みにくい）。"""
    idle = number_cell(1, None)

    assert idle.plain == "  1"
    assert len(idle.plain) == len(number_cell(1, Activity.RECORDING).plain)
    assert idle.spans == []


@pytest.mark.unit
def test_stage_cell_shows_running_work_instead_of_stage() -> None:
    session = _summary("20260101-000000", Stage.RECORDED)

    assert stage_cell(session, Activity.MINUTES, STAGE_LABELS).plain == "議事録生成中…"
    assert stage_cell(session, Activity.RECORDING, STAGE_LABELS).plain == "録音中（進行中）"


@pytest.mark.unit
def test_stage_cell_falls_back_to_stage_label() -> None:
    session = _summary("20260101-000000", Stage.MINUTES_DONE)

    assert stage_cell(session, None, STAGE_LABELS).plain == STAGE_LABELS[Stage.MINUTES_DONE]


# --- 録音中セッションの合成行 -----------------------------------------------------


@pytest.mark.unit
def test_merge_recording_session_adds_row_for_session_without_manifest() -> None:
    """録音中は manifest 未生成で一覧の母集団に入らないため、表示側で合成する。"""
    merged = merge_recording_session([], "20260804-101500")

    assert [s.session_id for s in merged] == ["20260804-101500"]
    assert merged[0].stage is Stage.NO_RECORDING
    assert merged[0].status == "録音中"
    assert merged[0].self_sec is None  # 録音長は確定していない


@pytest.mark.unit
def test_merge_recording_session_is_ranked_newest() -> None:
    """合成行は先頭に入る（呼び出し側はこの並びの先頭を既定選択に使う）。

    末尾に足すと「今まさに録っているセッション」が最古扱いになり、選択が外れたときに
    別セッションが既定になる（＝別会議へ課金・投稿する経路が戻ってくる）。
    """
    finished = [
        _summary("20260801-000000", Stage.MINUTES_DONE),
        _summary("20260803-000000", Stage.MINUTES_DONE),
    ]

    merged = merge_recording_session(finished, "20260804-170215")

    assert merged[0].session_id == "20260804-170215"


@pytest.mark.unit
def test_merge_recording_session_does_not_duplicate_existing_row() -> None:
    existing = [_summary("20260804-101500", Stage.RECORDED)]

    merged = merge_recording_session(existing, "20260804-101500")

    assert merged == existing


@pytest.mark.unit
def test_merge_recording_session_without_recording_is_unchanged() -> None:
    existing = [_summary("20260804-101500", Stage.RECORDED)]

    assert merge_recording_session(existing, None) == existing


# --- AWS 認証の表示 ---------------------------------------------------------------


@pytest.mark.unit
def test_auth_text_while_checking() -> None:
    assert auth_text(None) == "AWS 確認中…"


@pytest.mark.unit
def test_auth_text_shows_profile_and_region_when_valid() -> None:
    status = AuthStatus(AuthState.OK, profile="default", region="ap-northeast-1")

    assert auth_text(status) == "AWS default/ap-northeast-1 ✓"


@pytest.mark.unit
def test_auth_text_shows_reason_when_invalid() -> None:
    status = AuthStatus(AuthState.EXPIRED, profile="subtext", region="ap-northeast-1", message="…")

    assert auth_text(status) == "AWS subtext/ap-northeast-1 ✗ 認証切れ"


@pytest.mark.unit
def test_auth_text_when_check_itself_failed() -> None:
    # 確認できなかった場合は profile/region も無いため、状態だけを出す。
    assert auth_text(AuthStatus(AuthState.ERROR, message="uv が見つかりません")) == "AWS 確認失敗"


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


# --- 列幅（Content セルの自動幅計算が効かない問題への対処） -----------------------


@pytest.mark.unit
def test_column_widths_fit_session_id() -> None:
    """セッションIDが切れないこと（`Content` セルは自動幅計算で 1 セル扱いになる）。"""
    rows = [(Content("  1"), Content("20260804-170215"), Content("録音済"), "complete", "1.0", "1.0")]

    widths = column_widths(rows)

    assert widths[1] >= len("20260804-170215")


@pytest.mark.unit
def test_column_widths_reserve_space_for_running_labels() -> None:
    """処理中はセルだけ差し替わるため、その文字が入る幅を先に確保しておくこと。"""
    rows = [(Content("  1"), Content("s"), Content("録音済"), "complete", "1.0", "1.0")]

    widths = column_widths(rows)

    assert widths[2] >= len("議事録生成中…")


@pytest.mark.unit
def test_column_widths_never_below_header() -> None:
    widths = column_widths([])
    assert widths[4] >= len("self(s)")
