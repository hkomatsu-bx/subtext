"""ledger.py の単体テスト。追記の不変性・round-trip・月次集計・閾値判定を検証する。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meeting.ledger import (
    LedgerEntry,
    Thresholds,
    append,
    cumulative_after,
    evaluate,
    load,
    month_of,
    monthly_total,
    normalize_month,
)


def _entry(ts: str, est: float, *, stage: str = "transcribe", pii: bool = False) -> LedgerEntry:
    return LedgerEntry(
        ts=ts,
        session="20260625-120156",
        stage=stage,
        backend="aws",
        est_usd=est,
        unit_price_usd=0.024,
        cumulative_month_usd=est,
        pii_sent=pii,
        units={"minutes": 10.0},
    )


@pytest.mark.unit
def test_normalize_month_zero_pads_single_digit_month() -> None:
    """`2026-7` を `2026-07` へ揃えること。

    集計は `month_of(e.ts) == month` の文字列比較なので、ゼロ埋めが無いと 1 件も一致せず
    **実際には行があるのに $0 と報告される**（「使っていない」と読めてしまう）。
    """
    assert normalize_month("2026-7") == "2026-07"
    assert normalize_month(" 2026-07 ") == "2026-07"
    assert normalize_month("2026-12") == "2026-12"


@pytest.mark.unit
def test_normalize_month_rejects_unparsable_input() -> None:
    with pytest.raises(ValueError, match="YYYY-MM"):
        normalize_month("2026/07")


@pytest.mark.unit
def test_thresholds_from_config() -> None:
    th = Thresholds.from_config({"thresholds": {"per_run_usd": 5.0, "monthly_usd": 50.0}})
    assert th.per_run_usd == 5.0
    assert th.monthly_usd == 50.0


@pytest.mark.unit
def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load(tmp_path / "nope.jsonl") == []


@pytest.mark.unit
def test_append_then_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "cost-ledger.jsonl"
    e = _entry("2026-06-25T12:30:00+00:00", 2.89, stage="claude", pii=True)

    append(path, e)
    loaded = load(path)

    assert len(loaded) == 1
    assert loaded[0] == e
    assert loaded[0].pii_sent is True


@pytest.mark.unit
def test_append_is_additive_not_overwriting(tmp_path: Path) -> None:
    path = tmp_path / "cost-ledger.jsonl"
    append(path, _entry("2026-06-25T12:30:00+00:00", 1.0))
    append(path, _entry("2026-06-25T13:00:00+00:00", 2.0))

    loaded = load(path)
    assert len(loaded) == 2
    # 既存行が書き換えられていないこと（追記のみ）。
    assert [e.est_usd for e in loaded] == [1.0, 2.0]


@pytest.mark.unit
def test_json_keys_are_camelcase(tmp_path: Path) -> None:
    path = tmp_path / "cost-ledger.jsonl"
    append(path, _entry("2026-06-25T12:30:00+00:00", 2.89, stage="claude", pii=True))

    obj = json.loads(path.read_text(encoding="utf-8").strip())
    assert set(obj) >= {"unitPriceUsd", "estUsd", "cumulativeMonthUsd", "piiSent"}
    assert obj["piiSent"] is True


@pytest.mark.unit
def test_corrupt_line_raises_with_lineno(tmp_path: Path) -> None:
    path = tmp_path / "cost-ledger.jsonl"
    # 1行目は有効、2行目が壊れている → 行番号 "2 行目" を含む例外を期待。
    append(path, _entry("2026-06-25T00:00:00+00:00", 1.0))
    with path.open("a", encoding="utf-8") as fh:
        fh.write("not-json\n")
    with pytest.raises(ValueError, match="2 行目"):
        load(path)


@pytest.mark.unit
def test_blank_lines_ignored(tmp_path: Path) -> None:
    path = tmp_path / "cost-ledger.jsonl"
    append(path, _entry("2026-06-25T12:30:00+00:00", 1.0))
    # 末尾に空行を足しても件数は変わらない。
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n   \n")
    assert len(load(path)) == 1


@pytest.mark.unit
def test_month_of() -> None:
    assert month_of("2026-06-25T12:30:00+00:00") == "2026-06"


@pytest.mark.unit
def test_monthly_total_filters_by_month() -> None:
    entries = [
        _entry("2026-06-10T00:00:00+00:00", 1.5),
        _entry("2026-06-20T00:00:00+00:00", 2.0),
        _entry("2026-05-31T00:00:00+00:00", 9.0),
    ]
    assert monthly_total(entries, "2026-06") == 3.5
    assert monthly_total(entries, "2026-05") == 9.0
    assert monthly_total(entries, "2026-07") == 0.0


@pytest.mark.unit
def test_cumulative_after_adds_to_month_total() -> None:
    entries = [_entry("2026-06-10T00:00:00+00:00", 18.4)]
    assert cumulative_after(entries, "2026-06", 2.89) == pytest.approx(21.29)


@pytest.mark.unit
def test_evaluate_no_warning_under_thresholds() -> None:
    th = Thresholds(per_run_usd=5.0, monthly_usd=50.0)
    assert evaluate(2.0, 30.0, th) == []


@pytest.mark.unit
def test_evaluate_warns_on_per_run_and_monthly() -> None:
    th = Thresholds(per_run_usd=5.0, monthly_usd=50.0)
    warnings = evaluate(6.0, 55.0, th)
    assert len(warnings) == 2
    assert any("単発" in w for w in warnings)
    assert any("月次" in w for w in warnings)


@pytest.mark.unit
def test_evaluate_boundary_not_exceeded() -> None:
    # ちょうど上限は超過扱いにしない（> 判定）。
    th = Thresholds(per_run_usd=5.0, monthly_usd=50.0)
    assert evaluate(5.0, 50.0, th) == []
