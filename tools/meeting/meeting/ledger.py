"""コスト台帳（cost-ledger.jsonl）の追記・読込・月次集計・閾値判定。

1行1実行・追記のみ（audit.md 文化と整合、過去行は書き換えない）。記録するのは概算であり、
正は AWS Cost Explorer。Claude 経路は piiSent=true を必ず残す。

JSON キーは camelCase に固定（unitPriceUsd / estUsd / cumulativeMonthUsd / piiSent）。
時刻は呼び出し側が ISO8601(UTC) を与える seam とし（テスト容易性）、runner は now_iso() を使う。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

# 台帳に残す既定の注記（概算である旨を各行に明示）。
_DEFAULT_NOTE = "estimate; AWS Cost Explorer is source of truth"


@dataclass(frozen=True)
class Thresholds:
    """警告しきい値（USD）。超過しても停止はせず警告のみ（合意方針）。"""

    per_run_usd: float
    monthly_usd: float

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any]) -> "Thresholds":
        thresholds = cfg["thresholds"]
        return cls(
            per_run_usd=float(thresholds["per_run_usd"]),
            monthly_usd=float(thresholds["monthly_usd"]),
        )


@dataclass(frozen=True)
class LedgerEntry:
    """台帳1行。`units` は段ごとの根拠量（例: {"minutes": 238.4} / {"chars": 8000}）。"""

    ts: str  # ISO8601 UTC
    session: str
    stage: str  # transcribe / correct（C2 用語補正・Bedrock） / bedrock（要約） / claude
    backend: str  # aws / claude
    est_usd: float
    unit_price_usd: float
    cumulative_month_usd: float
    pii_sent: bool = False
    units: Mapping[str, float] = field(default_factory=dict)
    note: str = _DEFAULT_NOTE
    # 実行時に使った AWS プロファイル名（空＝未指定＝SDK 既定）。プロファイルの切替は課金先
    # アカウントが変わる操作のため、どの口座に出た支出かを後から辿れるように残す。秘密ではない。
    profile: str = ""

    def to_json_obj(self) -> dict[str, Any]:
        """JSONL の1行に書き出す camelCase dict へ変換。"""
        return {
            "ts": self.ts,
            "session": self.session,
            "stage": self.stage,
            "backend": self.backend,
            "units": dict(self.units),
            "unitPriceUsd": self.unit_price_usd,
            "estUsd": self.est_usd,
            "cumulativeMonthUsd": self.cumulative_month_usd,
            "piiSent": self.pii_sent,
            "profile": self.profile,
            "note": self.note,
        }

    @classmethod
    def from_json_obj(cls, obj: Mapping[str, Any]) -> "LedgerEntry":
        return cls(
            ts=str(obj["ts"]),
            session=str(obj["session"]),
            stage=str(obj["stage"]),
            backend=str(obj["backend"]),
            est_usd=float(obj["estUsd"]),
            unit_price_usd=float(obj["unitPriceUsd"]),
            cumulative_month_usd=float(obj["cumulativeMonthUsd"]),
            pii_sent=bool(obj.get("piiSent", False)),
            units={k: float(v) for k, v in dict(obj.get("units", {})).items()},
            note=str(obj.get("note", _DEFAULT_NOTE)),
            # 追記のみの台帳なので、キーを追加する前の行にはこの項目が無い（欠落は正常）。
            profile=str(obj.get("profile", "")),
        )


def now_iso() -> str:
    """現在時刻を ISO8601(UTC, 秒精度) 文字列で返す（runner 用の時刻 seam）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def month_of(ts: str) -> str:
    """ISO8601 タイムスタンプから 'YYYY-MM' を取り出す。"""
    return datetime.fromisoformat(ts).strftime("%Y-%m")


def normalize_month(raw: str) -> str:
    """人が入力した対象月を 'YYYY-MM' へ正規化する（`2026-7` → `2026-07`）。

    集計は `month_of(e.ts) == month` の文字列比較で行うため、ゼロ埋めされていない入力は
    **1 件も一致せず $0 と報告される**（実際には行があるのに「使っていない」と見える）。
    解釈できない書式は握りつぶさず ValueError（CLI が 1 行のエラーへ畳む）。
    """
    text = raw.strip()
    try:
        year, month = text.split("-", 1)
        return f"{int(year):04d}-{int(month):02d}"
    except ValueError as exc:
        raise ValueError(f"対象月の書式が不正です（YYYY-MM で指定してください）: {raw!r}") from exc


def load(path: Path) -> list[LedgerEntry]:
    """台帳を読み込む。空行は無視、壊れた行は行番号付きで例外（握りつぶさない）。"""
    if not path.exists():
        return []
    entries: list[LedgerEntry] = []
    text = path.read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
            entries.append(LedgerEntry.from_json_obj(obj))
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
            raise ValueError(f"台帳 {path} の {lineno} 行目が不正です: {exc}") from exc
    return entries


def append(path: Path, entry: LedgerEntry) -> None:
    """台帳に1行追記する（既存行は一切書き換えない）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry.to_json_obj(), ensure_ascii=False)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def has_entry(entries: list[LedgerEntry], session: str, stage: str) -> bool:
    """同一セッション・同一段の記録が既にあるか（再実行時の二重計上防止）。"""
    return any(e.session == session and e.stage == stage for e in entries)


def monthly_total(entries: list[LedgerEntry], month: str) -> float:
    """指定月(YYYY-MM)の概算合計 USD。"""
    total = sum(e.est_usd for e in entries if month_of(e.ts) == month)
    return round(total, 4)


def cumulative_after(entries: list[LedgerEntry], month: str, additional_usd: float) -> float:
    """既存月次合計に今回の概算を加えた累計（新エントリの cumulativeMonthUsd 用）。"""
    return round(monthly_total(entries, month) + additional_usd, 4)


def evaluate(per_run_usd: float, month_total_usd: float, thresholds: Thresholds) -> list[str]:
    """単発・月次の超過を判定し、警告メッセージの一覧を返す（空なら超過なし）。"""
    warnings: list[str] = []
    if per_run_usd > thresholds.per_run_usd:
        warnings.append(f"単発見積 ${per_run_usd:.4f} が上限 ${thresholds.per_run_usd:.2f} を超過")
    if month_total_usd > thresholds.monthly_usd:
        warnings.append(f"月次累計 ${month_total_usd:.4f} が上限 ${thresholds.monthly_usd:.2f} を超過")
    return warnings
