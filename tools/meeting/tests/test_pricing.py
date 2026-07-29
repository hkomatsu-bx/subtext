"""pricing.py の単体テスト。

概算値は本ファイル内で「式から再計算」して突き合わせる（暗算しない）。
さらに実 meeting.toml を tomllib で読み、from_config が通ることも検証する。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from meeting.pricing import Pricing

# テスト用の単価表（meeting.toml の既定値と同じ構造）。
_CFG = {
    "pricing": {
        "transcribe": {"usd_per_minute": 0.024},
        "bedrock": {"input_usd_per_1k": 0.003, "output_usd_per_1k": 0.015},
        "claude": {"input_usd_per_1k": 0.003, "output_usd_per_1k": 0.015},
    },
    "estimation": {"chars_per_token": 1.0, "summary_output_tokens": 1500, "correction_output_tokens": 500},
}

# tools/meeting/meeting.toml への相対パス（tests/ から1つ上）。
_MEETING_TOML = Path(__file__).resolve().parent.parent / "meeting.toml"


@pytest.fixture
def pricing() -> Pricing:
    return Pricing.from_config(_CFG)


@pytest.mark.unit
def test_from_config_binds_all_prices(pricing: Pricing) -> None:
    assert pricing.transcribe_usd_per_minute == 0.024
    assert pricing.bedrock_input_usd_per_1k == 0.003
    assert pricing.bedrock_output_usd_per_1k == 0.015
    assert pricing.claude_input_usd_per_1k == 0.003
    assert pricing.claude_output_usd_per_1k == 0.015
    assert pricing.chars_per_token == 1.0
    assert pricing.summary_output_tokens == 1500
    assert pricing.correction_output_tokens == 500


@pytest.mark.unit
def test_from_config_rejects_nonpositive_chars_per_token() -> None:
    bad = {**_CFG, "estimation": {**_CFG["estimation"], "chars_per_token": 0.0}}
    with pytest.raises(ValueError):
        Pricing.from_config(bad)


@pytest.mark.unit
def test_from_config_propagates_missing_key() -> None:
    with pytest.raises(KeyError):
        Pricing.from_config({"pricing": {}, "estimation": {}})


@pytest.mark.unit
def test_estimate_transcribe_sums_both_streams(pricing: Pricing) -> None:
    self_sec, others_sec = 120.4, 118.0
    # paired は2系統合算: (120.4 + 118.0) / 60 分 × 0.024 USD/分
    expected = round((self_sec + others_sec) / 60.0 * 0.024, 4)

    est = pricing.estimate_transcribe(self_sec, others_sec)

    assert est.stage == "transcribe"
    assert est.usd == pytest.approx(expected)
    assert est.pii_sent is False


@pytest.mark.unit
def test_estimate_transcribe_zero_is_zero(pricing: Pricing) -> None:
    assert pricing.estimate_transcribe(0.0, 0.0).usd == 0.0


@pytest.mark.unit
def test_estimate_transcribe_rejects_negative(pricing: Pricing) -> None:
    with pytest.raises(ValueError):
        pricing.estimate_transcribe(-1.0, 10.0)


@pytest.mark.unit
def test_estimate_bedrock_input_plus_output(pricing: Pricing) -> None:
    chars = 8000
    # 入力: 8000字 / 1.0 = 8000tok → 8000/1000 × 0.003
    # 出力: 1500tok → 1500/1000 × 0.015
    expected = round(8000 / 1000 * 0.003 + 1500 / 1000 * 0.015, 4)

    est = pricing.estimate_bedrock(chars)

    assert est.stage == "bedrock"
    assert est.usd == pytest.approx(expected)
    assert est.pii_sent is False


@pytest.mark.unit
def test_estimate_correct_is_a_separate_bedrock_call(pricing: Pricing) -> None:
    """C2 用語補正段は要約段とは別の Bedrock 呼び出しとして独立に見積もること。

    既定経路（Bedrock 生成）は要約の前に補正でも全文を Bedrock へ送る。ここを数えないと
    1 回の実行で発生する Bedrock 支出の約半分が見積・台帳から漏れる。
    """
    chars = 8000
    # 入力: 8000字 / 1.0 = 8000tok → 8000/1000 × 0.003 ／ 出力: 500tok → 500/1000 × 0.015
    expected = round(8000 / 1000 * 0.003 + 500 / 1000 * 0.015, 4)

    est = pricing.estimate_correct(chars)

    assert est.stage == "correct"
    assert est.usd == pytest.approx(expected)
    assert est.pii_sent is False
    # 補正段だけで要約段の半分を超える額になる（＝無視できないことの明示）。
    assert est.usd > pricing.estimate_bedrock(chars).usd * 0.5


@pytest.mark.unit
def test_estimate_claude_marks_pii_sent(pricing: Pricing) -> None:
    chars = 8000
    expected = round(8000 / 1000 * 0.003 + 1500 / 1000 * 0.015, 4)

    est = pricing.estimate_claude(chars)

    assert est.stage == "claude"
    assert est.usd == pytest.approx(expected)
    # Claude 経路は実名 PII を外部送信する → 台帳に必ず残すフラグ。
    assert est.pii_sent is True


@pytest.mark.unit
def test_estimate_summary_rejects_negative_chars(pricing: Pricing) -> None:
    with pytest.raises(ValueError):
        pricing.estimate_bedrock(-1)


@pytest.mark.unit
def test_real_meeting_toml_loads(pricing: Pricing) -> None:
    # 実ファイルの構造が from_config と整合していることを担保する。
    with _MEETING_TOML.open("rb") as fh:
        cfg = tomllib.load(fh)
    loaded = Pricing.from_config(cfg)
    assert loaded.transcribe_usd_per_minute > 0
    assert loaded.summary_output_tokens > 0
    assert loaded.correction_output_tokens > 0
