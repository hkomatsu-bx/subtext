"""SC-P3 構造化議事録の客観検証（ISS-04）。

Unit B が生成した `minutes.md` が SC-P3 / BR-SUM-01 の構造要件を満たすか検証する:

- 必須3セクションが `## 見出し` として存在する: 決定事項 / ToDo / 論点・議論サマリ
- 各セクションが空でない（見出しだけで本文が無い状態を検出）
- ToDo が `- [ ] 内容（担当 / 期限）` 形式の項目を含む（BR-SUM-01）
- 部分録音注記（BR-SUM-05）の有無を報告（incomplete セッション由来か）
- 末尾が途中で切れていないか（max_tokens 到達による截断の簡易検出）

「そのまま読めるか」の最終的な読みやすさは定性判断のため、構造の機械判定＋人手確認項目に分ける。
議事録本文は PII を含みうるため、出力には本文を**転記しない**（NFR-SEC-04）。集計・有無のみ報告する。

依存なし（標準ライブラリのみ）。実行:
`python tools/verify/validate_minutes.py <minutes.md または out/session ディレクトリ>`
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Windows コンソール(cp932)で日本語/記号出力がクラッシュしないよう UTF-8 へ固定する。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

REQUIRED_SECTIONS = ("決定事項", "ToDo", "論点・議論サマリ")
PARTIAL_NOTE_PREFIX = "> 注記:"
# `## 見出し` 行（前後空白許容）。
_HEADING_RE = re.compile(r"^\s*##\s+(?P<title>.+?)\s*$")
# ToDo チェックボックス項目。
_TODO_ITEM_RE = re.compile(r"^\s*-\s*\[[ xX]\]\s+\S")

# 検証1項目の結果: (マーク, 表示行)。マーク "" は情報行（判定に影響しない）。
CheckResult = tuple[str, str]


def _split_sections(markdown: str) -> dict[str, str]:
    """`## 見出し` 単位で本文を分割する（純粋）。見出し名→本文（次見出しまで）。"""
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []

    def flush() -> None:
        if current is not None:
            sections[current] = "\n".join(buf).strip()

    for raw in markdown.splitlines():
        m = _HEADING_RE.match(raw)
        if m:
            flush()  # 直前セクションを確定してから次を開始（ガード節で1段浅く）
            current = m.group("title")
            buf = []
        elif current is not None:
            buf.append(raw)
    flush()
    return sections


def _resolve_minutes(arg: Path) -> Path:
    if arg.is_dir():
        return arg / "minutes.md"
    return arg


def _check(label: str, ok: bool | None, detail: str) -> CheckResult:
    mark = "OK " if ok else ("▲ " if ok is None else "NG ")
    return mark, f"  [{mark.strip()}] {label}: {detail}"


def _check_required_sections(sections: dict[str, str]) -> list[CheckResult]:
    """必須3セクションが存在し本文が空でないかを検査する（BR-SUM-01）。"""
    results: list[CheckResult] = []
    for required in REQUIRED_SECTIONS:
        present = required in sections
        non_empty = present and bool(sections[required])
        detail = "存在しない" if not present else ("見出しのみで本文が空" if not non_empty else "本文あり")
        results.append(_check(f"## {required}", present and non_empty, detail))
    return results


def _check_todo_format(sections: dict[str, str]) -> CheckResult:
    """ToDo が `- [ ]` 項目を含むか検査する（本文は出さず件数のみ・BR-SUM-01）。"""
    todo_body = sections.get("ToDo", "")
    items = [ln for ln in todo_body.splitlines() if _TODO_ITEM_RE.match(ln)]
    suffix = "（0件＝該当なし注記か未整形か要確認）" if not items else ""
    return _check("ToDo チェックボックス形式", None if not items else True, f"`- [ ]` 項目 {len(items)}件{suffix}")


def _partial_note_line(markdown: str) -> str:
    """部分録音注記（BR-SUM-05）の有無を情報行として返す（合否ではない）。"""
    has_note = PARTIAL_NOTE_PREFIX in markdown
    return f"  [情報] 部分録音注記(BR-SUM-05): {'あり（incomplete 由来）' if has_note else 'なし'}"


def _check_truncation(markdown: str) -> CheckResult:
    """末尾が文末記号で終わらない＝max_tokens 截断の疑いを検出する（▲・要確認）。"""
    tail = markdown.rstrip()
    suspect = bool(tail) and tail[-1] not in "。.!?！？)）」』*_`#-"
    detail = "末尾が文末記号で終わっていない（max_tokens 截断の疑い・要確認）" if suspect else "末尾は自然に終端"
    return _check("末尾の截断", None if suspect else True, detail)


def _print_report(path: Path, found_titles: list[str], results: list[CheckResult]) -> None:
    """検査結果と人手確認項目を整形出力する（本文は転記しない・NFR-SEC-04）。"""
    print("=" * 64)
    print(f"SC-P3 議事録構造 検証 - {path}")
    print("=" * 64)
    print(f"検出セクション: {found_titles}")
    print("\n".join(line for _, line in results))
    print("-" * 64)
    print("人手確認（定性・本スクリプトでは判定不可）:")
    print("  [▲] 内容がそのまま読める日本語か（誤情報の創作が無いか・BR-SUM 趣旨）")
    print("  [▲] 発言者ラベル(spk_0 等)が残る場合の可読性")
    print("-" * 64)


def validate(path: Path) -> int:
    """SC-P3 機械判定。戻り値 0=PASS/要確認, 1=必須セクション欠落・空（FAIL）。"""
    if not path.exists():
        print(f"NG: minutes.md が見つかりません: {path}")
        print("    → Unit B パイプラインが summarize 段まで完走していない可能性（NAMING_REQUIRED 停止など）。")
        return 1

    try:
        markdown = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"NG: 読み込み失敗: {exc}")
        return 1

    sections = _split_sections(markdown)
    results = _check_required_sections(sections)
    results.append(_check_todo_format(sections))
    results.append(("", _partial_note_line(markdown)))  # 情報行（判定に影響しない）
    results.append(_check_truncation(markdown))

    _print_report(path, list(sections.keys()), results)

    has_ng = any(mark.strip() == "NG" for mark, _ in results)
    verdict = "FAIL（必須セクション欠落/空）" if has_ng else "PASS（構造は合格・上記 ▲ を人手確認）"
    print(f"判定: {verdict}")
    return 1 if has_ng else 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print(__doc__)
        print("引数エラー: minutes.md または out/<session> ディレクトリのパスを1つ指定してください。")
        return 2
    return validate(_resolve_minutes(Path(args[0])))


if __name__ == "__main__":
    sys.exit(main())
