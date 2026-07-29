"""naming.py の単体テスト（話者名ゲートの記入対象抽出と書き戻し規則）。

CLI（wizard の対話入力）と TUI（モーダル入力）が共有する規則なので、ここで規則そのものを固定する。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meeting import naming

_DATA = {
    "sessionId": "20260625-120156",
    "mappings": {"spk_0": "", "spk_1": "既に記入済", "spk_2": "   ", "self": "自分"},
    "unresolved": [],
    "_clusters": [
        {"label": "spk_0", "segmentCount": 42, "sampleUtterances": ["では始めましょう"]},
        {"label": "spk_1", "segmentCount": 31, "sampleUtterances": ["はい"]},
    ],
}


@pytest.mark.unit
def test_pending_prompts_lists_only_blank_others_labels() -> None:
    labels = [p.label for p in naming.pending_prompts(_DATA)]

    # 空文字と空白のみが対象。既記入（spk_1）と self は除く。
    assert labels == ["spk_0", "spk_2"]


@pytest.mark.unit
def test_pending_prompts_carries_cluster_hints() -> None:
    prompt = naming.pending_prompts(_DATA)[0]

    assert prompt.segment_count == 42
    assert prompt.samples == ("では始めましょう",)


@pytest.mark.unit
def test_pending_prompts_without_cluster_still_needs_input() -> None:
    """`_clusters` に対応が無いラベル（spk_2）も記入対象に残る（手がかりが無いだけ）。"""
    prompt = next(p for p in naming.pending_prompts(_DATA) if p.label == "spk_2")

    assert prompt.segment_count == "?"
    assert prompt.samples == ()


@pytest.mark.unit
def test_pending_prompts_tolerates_null_mappings() -> None:
    """キーはあるが JSON null のケース（dict(None) で落ちないこと）。"""
    assert naming.pending_prompts({"mappings": None, "_clusters": None}) == []


# ライブ字幕/Teams 由来の「候補（仮名）」が自動補完された状態。値は入っているが人は未確認。
_HINTED = {
    "sessionId": "live",
    "mappings": {"spk_0": "自分", "spk_1": "相手", "spk_2": "Speaker 1", "self": "自分"},
    "unresolved": [],
    "_hintedLabels": ["spk_0", "spk_1", "spk_2"],
}


@pytest.mark.unit
def test_pending_prompts_includes_unconfirmed_hints() -> None:
    """自動補完された候補は人が確認するまで記入待ちに残ること。

    ライブ字幕 VTT の話者名は Unit C の表示ラベル（「自分」「相手」）、Teams は「Speaker 1」に
    なり得る。値の有無だけで確定と判断すると命名ゲート（BR-NAME-01）が素通りし、TUI はモーダルを
    出さずに課金パイプラインを再開して、参加者全員が仮名 1 人に統合された議事録が黙って出来る。
    """
    prompts = naming.pending_prompts(_HINTED)

    assert [p.label for p in prompts] == ["spk_0", "spk_1", "spk_2"]
    assert [p.current for p in prompts] == ["自分", "相手", "Speaker 1"]  # 候補は初期値として提示


@pytest.mark.unit
def test_apply_names_confirms_hint_on_blank_input() -> None:
    """候補があるラベルの空入力は「候補のまま確定」として扱い、以後は記入待ちにしないこと。"""
    result = naming.apply_names(_HINTED, {"spk_0": "", "spk_1": "", "spk_2": ""})

    assert naming.applied_count(_HINTED, {"spk_0": "", "spk_1": "", "spk_2": ""}) == 3
    assert result["mappings"]["spk_1"] == "相手"
    assert naming.pending_prompts(result) == []
    assert naming.HINTED_LABELS_KEY not in result


@pytest.mark.unit
def test_apply_names_overwrites_hint_with_real_name() -> None:
    result = naming.apply_names(_HINTED, {"spk_1": "田中太郎"})

    assert result["mappings"]["spk_1"] == "田中太郎"
    # 確認したラベルだけを候補から外す（残りは引き続き記入待ち）。
    assert result[naming.HINTED_LABELS_KEY] == ["spk_0", "spk_2"]
    assert [p.label for p in naming.pending_prompts(result)] == ["spk_0", "spk_2"]


@pytest.mark.unit
def test_apply_names_fills_pending_label() -> None:
    result = naming.apply_names(_DATA, {"spk_0": "小松"})

    assert result["mappings"]["spk_0"] == "小松"


@pytest.mark.unit
def test_apply_names_is_non_destructive() -> None:
    naming.apply_names(_DATA, {"spk_0": "小松"})

    assert _DATA["mappings"]["spk_0"] == ""  # 元の dict は変わらない。


@pytest.mark.unit
def test_apply_names_preserves_existing_and_self() -> None:
    """既記入ラベルと self への指定は無視する（人手の誤操作で既存名を壊さない）。"""
    result = naming.apply_names(_DATA, {"spk_1": "上書き", "self": "別名"})

    assert result["mappings"]["spk_1"] == "既に記入済"
    assert result["mappings"]["self"] == "自分"


@pytest.mark.unit
def test_apply_names_skips_blank_input() -> None:
    result = naming.apply_names(_DATA, {"spk_0": "   "})

    assert result["mappings"]["spk_0"] == ""  # 元のラベルのまま（spk_0 表記で残る）。


@pytest.mark.unit
def test_apply_names_strips_surrounding_whitespace() -> None:
    result = naming.apply_names(_DATA, {"spk_0": "  小松  "})

    assert result["mappings"]["spk_0"] == "小松"


@pytest.mark.unit
def test_apply_names_keeps_unknown_keys() -> None:
    """`_clusters` など Unit B 由来の他のキーを落とさないこと（再実行で手がかりが消える）。"""
    result = naming.apply_names(_DATA, {"spk_0": "小松"})

    assert result["_clusters"] == _DATA["_clusters"]
    assert result["sessionId"] == _DATA["sessionId"]


@pytest.mark.unit
def test_applied_count_counts_only_effective_entries() -> None:
    count = naming.applied_count(_DATA, {"spk_0": "小松", "spk_2": "  ", "spk_1": "上書き", "self": "別名"})

    assert count == 1  # spk_0 のみ有効。


@pytest.mark.unit
def test_load_and_save_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "speaker_names.json"
    naming.save(path, _DATA)

    assert naming.load(path) == _DATA
    # 非 ASCII をエスケープせず書く（人が直接編集する前提のファイル）。
    assert "自分" in path.read_text(encoding="utf-8")


@pytest.mark.unit
def test_load_raises_actionable_error_on_broken_json(tmp_path: Path) -> None:
    path = tmp_path / "speaker_names.json"
    path.write_text("{壊れている", encoding="utf-8")

    with pytest.raises(ValueError, match="読込に失敗"):
        naming.load(path)


@pytest.mark.unit
def test_save_raises_actionable_error_when_path_is_a_directory(tmp_path: Path) -> None:
    path = tmp_path / "speaker_names.json"
    path.mkdir()

    with pytest.raises(ValueError, match="書込に失敗"):
        naming.save(path, _DATA)


@pytest.mark.unit
def test_pending_prompts_empty_after_apply(tmp_path: Path) -> None:
    """記入後は同じデータから記入待ちが消える（ゲートが再度出ないこと）。"""
    filled = naming.apply_names(_DATA, {"spk_0": "小松", "spk_2": "佐藤"})

    assert naming.pending_prompts(filled) == []
    # Unit B が読める JSON として成立すること。
    assert json.loads(json.dumps(filled))["mappings"]["spk_2"] == "佐藤"
