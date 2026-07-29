"""SC-P4 / ISS-05 ライブ字幕の遅延・partial安定性の解析。

Unit C（`Subtext.Live.exe`）のコンソール出力をリダイレクトしたキャプチャファイルを解析する。
Unit C の出力行は CaptionRenderer.FormatLine 由来:

    [MM:SS] [自分|相手]<mark> <text>  (Δ<N>ms)      mark: '~'=partial / ' '=final
    遅延サマリ: final <N>件 / 平均 <avg>ms / 最大 <max>ms      ← 終了時サマリ

本スクリプトは行を構造化し、系統別に以下を算出する:
- final 遅延の分布（件数 / 平均 / 中央値 / p90 / 最大 / 最小）— SC-P4 の数値根拠
- partial / final 件数、final 1件あたりの先行 partial 数（partial→final 安定性の代理指標）
- イベント時刻オフセットの連続ギャップ（長い停滞＝フリーズ候補）— ISS-05 の挙動観測
- アプリ出力の「遅延サマリ」行との突合（計測一貫性チェック）

【PII 注意・NFR-SEC-04】字幕本文は PII を含みうる。本スクリプトは **本文を一切出力しない**（Δms と
件数・系統のみ）。キャプチャ採取は**実会議ではなく非機密のテスト発話**で行い、解析後にファイルを削除すること
（ランブック参照）。

依存なし（標準ライブラリのみ）。実行: `python tools/verify/analyze_live_latency.py <capture.txt>`
"""

from __future__ import annotations

import itertools
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

# Windows コンソール(cp932)で日本語/記号出力がクラッシュしないよう UTF-8 へ固定する。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 長い無出力ギャップ（秒）。これを超える連続イベント時刻差をフリーズ候補として報告。
FREEZE_GAP_WARN_SEC = 5.0

# 分は 2 桁固定にしない。100 分を超える会議（`[100:05]`）の行を全部落としてしまう
# （長時間会議こそフリーズ・遅延の観測対象なので、そこだけ解析対象外になるのは致命的）。
_LINE_RE = re.compile(r"^\[(?P<mm>\d{2,}):(?P<ss>\d{2})\] \[(?P<spk>自分|相手)\](?P<mark>[ ~]) (?P<rest>.*?)\s*$")
_LATENCY_RE = re.compile(r"\(Δ(?P<ms>\d+(?:\.\d+)?)ms\)\s*$")
_SUMMARY_RE = re.compile(
    r"遅延サマリ:\s*final\s*(?P<count>\d+)件\s*/\s*平均\s*(?P<avg>\d+(?:\.\d+)?)ms\s*/\s*最大\s*(?P<max>\d+(?:\.\d+)?)ms"
)


@dataclass(frozen=True)
class Event:
    """1字幕イベント（本文は保持しない＝PII 非保持）。"""

    offset_sec: int
    speaker: str
    is_partial: bool
    latency_ms: float | None


def parse(text: str) -> tuple[list[Event], str | None]:
    """キャプチャ全文を Event 列とサマリ行へ分解する（純粋・本文は破棄）。"""
    events: list[Event] = []
    summary: str | None = None
    for raw in text.splitlines():
        sm = _SUMMARY_RE.search(raw)
        if sm:
            summary = raw.strip()
            continue
        m = _LINE_RE.match(raw)
        if not m:
            continue
        offset = int(m.group("mm")) * 60 + int(m.group("ss"))
        rest = m.group("rest")
        lm = _LATENCY_RE.search(rest)
        latency = float(lm.group("ms")) if lm else None
        events.append(Event(offset, m.group("spk"), m.group("mark") == "~", latency))
    return events, summary


def _dist(values: list[float]) -> str:
    """遅延サンプル列を「件数/平均/中央/p90/最大/最小」の1行サマリ文字列へ整形する。"""
    if not values:
        return "サンプル無し"
    s = sorted(values)
    p90 = s[min(len(s) - 1, int(round(0.9 * (len(s) - 1))))]
    return (
        f"件数 {len(s)} / 平均 {statistics.mean(s):.0f}ms / 中央 {statistics.median(s):.0f}ms "
        f"/ p90 {p90:.0f}ms / 最大 {max(s):.0f}ms / 最小 {min(s):.0f}ms"
    )


def _freeze_gaps(events: list[Event]) -> list[tuple[int, int, int]]:
    """連続イベントの時刻ギャップが閾値超の区間 (前offset, 次offset, 差) を返す。

    隣接ペア走査は `itertools.pairwise` で宣言的に行う（EP Item 24 / §15）。
    events が1件以下なら pairwise は空を返すため、従来の prev 手追跡と等価。
    """
    return [
        (a.offset_sec, b.offset_sec, b.offset_sec - a.offset_sec)
        for a, b in itertools.pairwise(events)
        if b.offset_sec - a.offset_sec > FREEZE_GAP_WARN_SEC
    ]


def _print_speaker_breakdown(finals: list[Event], partials: list[Event]) -> None:
    """系統（自分/相手）ごとに final/partial 件数・遅延分布・partial 比を出力する。"""
    for spk in ("自分", "相手"):
        spk_finals = [e for e in finals if e.speaker == spk]
        spk_partials = [e for e in partials if e.speaker == spk]
        lat = [e.latency_ms for e in spk_finals if e.latency_ms is not None]
        print(f"[{spk}] final {len(spk_finals)} / partial {len(spk_partials)}")
        print(f"    final 遅延: {_dist(lat)}")
        if spk_finals:
            ratio = len(spk_partials) / len(spk_finals)
            print(f"    partial/final 比: {ratio:.1f}（final 1件あたり先行 partial 数の目安＝更新の揺れ）")


def _print_freeze_report(events: list[Event]) -> None:
    """連続イベントの長い無出力ギャップ（フリーズ候補）を出力する（ISS-05）。"""
    gaps = _freeze_gaps(events)
    if not gaps:
        print(f"フリーズ候補: なし（連続イベント間ギャップは全て {FREEZE_GAP_WARN_SEC:.0f}s 以内）")
        return
    print(f"フリーズ候補（{FREEZE_GAP_WARN_SEC:.0f}s 超の無出力ギャップ） {len(gaps)}件:")
    for a, b, d in gaps[:10]:
        print(f"    {a // 60:02d}:{a % 60:02d} → {b // 60:02d}:{b % 60:02d}（{d}s 無出力）")


def _print_consistency(summary: str | None, final_count: int) -> None:
    """アプリサマリの final 件数とスクリプト集計を突合し、不一致なら注意を出す。"""
    if not summary:
        return
    sm = _SUMMARY_RE.search(summary)
    if sm and int(sm.group("count")) != final_count:
        print(
            f"  ▲ 注意: サマリ final件数 {sm.group('count')} とスクリプト集計 {final_count} が不一致"
            "（partial の Δ 付与有無や行欠損の可能性）。"
        )


def _print_human_checks() -> None:
    """定性の人手確認項目を出力する（ISS-05）。"""
    print("人手確認（定性・ISS-05）:")
    print("  [▲] partial→final の更新が自然か（過度な揺れ・確定遅れがないか）")
    print("  [▲] 自分/相手のラベルが正しく付くか、許容できる遅延に体感されるか")
    print("  [▲] AudioStreamPublisher の pull 起因の詰まり/打ち切りが無いか")


def analyze(path: Path) -> int:
    """キャプチャファイルを解析し遅延分布・フリーズ候補・突合結果を出力する（終了コード 0=成功 / 1=失敗）。"""
    if not path.exists():
        print(f"NG: キャプチャファイルが見つかりません: {path}")
        return 1
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"NG: 読み込み失敗: {exc}")
        return 1

    events, summary = parse(text)
    if not events:
        print("NG: 字幕行を1件も解析できませんでした。")
        print("    → .exe 直起動で実行したか（dotnet run だと Ctrl+C で停止せずサマリも出ない・ISS-14）、")
        print("      出力フォーマットが想定どおりか確認してください。")
        return 1

    finals = [e for e in events if not e.is_partial]
    partials = [e for e in events if e.is_partial]

    print("=" * 64)
    print(f"SC-P4 / ISS-05 ライブ字幕 遅延・安定性 解析 - {path}")
    print("=" * 64)
    print(f"総イベント: {len(events)}（final {len(finals)} / partial {len(partials)}）")
    print(
        f"アプリ出力サマリ: {summary}"
        if summary
        else "アプリ出力サマリ: 見つからず（.exe 直起動での Ctrl+C 停止を推奨・ISS-14）"
    )
    print("-" * 64)
    _print_speaker_breakdown(finals, partials)
    print("-" * 64)
    _print_freeze_report(events)
    _print_consistency(summary, len(finals))
    print("-" * 64)
    _print_human_checks()
    print("-" * 64)
    print("判定: 上記の遅延分布・フリーズ候補・▲ を基に SC-P4/ISS-05 の go/no-go を人手で決定")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print(__doc__)
        print("引数エラー: ライブ出力キャプチャファイルのパスを1つ指定してください。")
        return 2
    return analyze(Path(args[0]))


if __name__ == "__main__":
    sys.exit(main())
