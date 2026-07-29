"""CLI のフラグ解決ポリシーの単体テスト（純粋ロジック・AWS 不要）。

`--no-summarize`（議事録を Claude 等で外部生成する経路）は C2 用語補正段も
自動でスキップする＝Bedrock を一切踏まないことを保証する（FR-17 / C1 是正）。
"""

from __future__ import annotations

from argparse import Namespace

import pytest

from subtext_postmeeting.cli import _reject_conflicting_flags, _resolve_stage_flags
from subtext_postmeeting.errors import PipelineError
from subtext_postmeeting.models import Stage


def test_default_runs_both_stages() -> None:
    # フラグなし: 補正も要約も実行（既定の Bedrock 経路）。
    assert _resolve_stage_flags(no_summarize=False, no_correct=False) == (True, True)


def test_no_correct_only_skips_correction() -> None:
    # --no-correct 単独: 補正だけスキップ、要約は実行。
    assert _resolve_stage_flags(no_summarize=False, no_correct=True) == (True, False)


def test_no_summarize_also_skips_correction() -> None:
    # --no-summarize は補正段も自動スキップ → Bedrock 未課金（VTT 経路の課金ゼロ・FR-17）。
    assert _resolve_stage_flags(no_summarize=True, no_correct=False) == (False, False)


def test_no_summarize_and_no_correct_both_off() -> None:
    # 両指定は --no-summarize と同結果（冗長だが無害）。
    assert _resolve_stage_flags(no_summarize=True, no_correct=True) == (False, False)


def _args(*, no_summarize: bool = False, no_correct: bool = False) -> Namespace:
    return Namespace(no_summarize=no_summarize, no_correct=no_correct)


def test_reject_no_summarize_with_stage_summarized() -> None:
    # --no-summarize（要約を踏まない）と --stage summarized（要約だけ踏む）は矛盾。
    with pytest.raises(PipelineError):
        _reject_conflicting_flags(_args(no_summarize=True), Stage.SUMMARIZED)


def test_reject_no_correct_with_stage_corrected() -> None:
    # --no-correct（補正を踏まない）と --stage corrected（補正だけ踏む）は矛盾。
    with pytest.raises(PipelineError):
        _reject_conflicting_flags(_args(no_correct=True), Stage.CORRECTED)


def test_reject_no_summarize_with_stage_corrected() -> None:
    # --no-summarize は補正段も自動スキップするため --stage corrected とも矛盾する。
    with pytest.raises(PipelineError):
        _reject_conflicting_flags(_args(no_summarize=True), Stage.CORRECTED)


def test_no_conflict_passes() -> None:
    # 矛盾のない組み合わせは例外を出さない。
    assert _reject_conflicting_flags(_args(), Stage.SUMMARIZED) is None
    assert _reject_conflicting_flags(_args(no_summarize=True), None) is None
