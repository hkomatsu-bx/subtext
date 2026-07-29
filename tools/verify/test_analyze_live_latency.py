"""analyze_live_latency（SC-P4/ISS-05 ライブ字幕 遅延解析）の単体テスト。

純粋部（parse / _dist / _freeze_gaps）と analyze の集計・PII 非出力を検証する。
実行: uv run pytest tools/verify/test_analyze_live_latency.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import analyze_live_latency as al  # noqa: E402


# --- parse -----------------------------------------------------------------
def test_parse_extracts_events_and_summary() -> None:
    # final 行は Unit C 書式で mark=空白＋区切り空白＝`]` の後に空白2つ、partial は `]~ `。
    text = "\n".join(
        [
            "[00:01] [自分]~ こんにち (Δ100ms)",  # partial
            "[00:02] [自分]  こんにちは (Δ150ms)",  # final
            "[00:05] [相手]  はい (Δ80ms)",
            "遅延サマリ: final 2件 / 平均 115ms / 最大 150ms",
            "解析対象外のゴミ行",
        ]
    )

    events, summary = al.parse(text)

    assert len(events) == 3
    assert summary is not None and "遅延サマリ" in summary
    assert events[0].is_partial and events[0].speaker == "自分" and events[0].latency_ms == 100.0
    assert events[1].is_partial is False and events[1].offset_sec == 2
    assert events[2].speaker == "相手"


def test_parse_ignores_non_matching_lines() -> None:
    events, summary = al.parse("random\nlines\nwith no markers\n")

    assert events == []
    assert summary is None


def test_parse_final_without_latency() -> None:
    events, _ = al.parse("[00:03] [相手]  遅延情報なしの行")

    assert len(events) == 1
    assert events[0].latency_ms is None


# --- _dist / _freeze_gaps --------------------------------------------------
def test_dist_empty() -> None:
    assert al._dist([]) == "サンプル無し"


def test_dist_reports_stats() -> None:
    result = al._dist([100.0, 200.0, 300.0])

    assert "件数 3" in result
    assert "最大 300ms" in result
    assert "最小 100ms" in result


def test_freeze_gaps_detects_long_gap() -> None:
    events = [al.Event(0, "自分", False, None), al.Event(10, "自分", False, None)]

    assert al._freeze_gaps(events) == [(0, 10, 10)]


def test_freeze_gaps_none_when_within_threshold() -> None:
    events = [al.Event(0, "自分", False, None), al.Event(3, "自分", False, None)]

    assert al._freeze_gaps(events) == []


# --- analyze ---------------------------------------------------------------
def test_analyze_missing_file(tmp_path: Path, capsys) -> None:
    rc = al.analyze(tmp_path / "nope.txt")

    assert rc == 1
    assert "見つかりません" in capsys.readouterr().out


def test_analyze_no_events(tmp_path: Path, capsys) -> None:
    path = tmp_path / "cap.txt"
    path.write_text("字幕行を含まないキャプチャ\n", encoding="utf-8")

    rc = al.analyze(path)

    assert rc == 1
    assert "1件も解析できませんでした" in capsys.readouterr().out


def test_analyze_reports_distribution_and_freeze(tmp_path: Path, capsys) -> None:
    """検出したフリーズ候補が件数と区間付きで報告されること。

    「フリーズ候補」という語は検出なしの行（`フリーズ候補: なし（…）`）にも含まれるため、
    部分一致だけを見ると検出そのものを無効化してもテストが緑になる。件数と区間で固定する。
    """
    path = tmp_path / "cap.txt"
    path.write_text(
        "\n".join(
            [
                "[00:01] [自分]~ x (Δ100ms)",
                "[00:02] [自分]  xy (Δ150ms)",
                "[00:20] [相手]  z (Δ80ms)",  # 前イベントから 18s → フリーズ候補
                "遅延サマリ: final 2件 / 平均 115ms / 最大 150ms",
            ]
        ),
        encoding="utf-8",
    )

    rc = al.analyze(path)

    out = capsys.readouterr().out
    assert rc == 0
    assert "SC-P4" in out
    assert "フリーズ候補（5s 超の無出力ギャップ） 1件" in out
    assert "00:02 → 00:20（18s 無出力）" in out
    assert "なし" not in out


def test_parse_accepts_offsets_beyond_100_minutes() -> None:
    """100 分超の行を落とさないこと（分を 2 桁固定にすると長時間会議が全部消える）。

    長時間会議こそフリーズ・遅延の観測対象なので、そこだけ解析対象外になると解析の意味が無い。
    """
    events, _ = al.parse("[100:05] [相手]  ろんぐ (Δ120ms)\n[09:59] [自分]  みじかい (Δ80ms)")

    assert [e.offset_sec for e in events] == [100 * 60 + 5, 9 * 60 + 59]


def test_analyze_does_not_leak_caption_text(tmp_path: Path, capsys) -> None:
    # 字幕本文は PII を含みうる。出力には Δms・件数のみで本文を一切出さない（NFR-SEC-04）。
    path = tmp_path / "cap.txt"
    path.write_text(
        "[00:01] [自分]  ヒミツの発言内容 (Δ100ms)\n遅延サマリ: final 1件 / 平均 100ms / 最大 100ms\n",
        encoding="utf-8",
    )

    al.analyze(path)

    assert "ヒミツ" not in capsys.readouterr().out


# --- main ------------------------------------------------------------------
def test_main_requires_single_arg(capsys) -> None:
    assert al.main([]) == 2
