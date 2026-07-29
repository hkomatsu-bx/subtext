"""SC-P1 録音/同期の客観検証（ISS-04）。

Unit A の録音セッション（`recordings/<session>/`）の `manifest.json` と各 WAV ヘッダを読み、
SC-P1 のうち**機械判定できる項目**を検証する:

- status=complete か（ISS-14 の異常終了＝incomplete/manifest 不在を検出）
- streams が self / others のちょうど2件か（BR-IO-03）
- 各 WAV が 16kHz / mono / 16bit PCM か（FR-05 整合）
- WAV ヘッダが有効か（RIFF/data>0。ISS-14 のヘッダ未確定を検出）
- meta.durationSec と WAV 実測長の整合
- 無音補填量 silenceFilledSec とその比率（SyncMath による同期健全性, FR-04）

「通知音・他アプリ音の混入が許容範囲か」「会議に参加せず取得できたか」は内容/運用の定性判断のため
本スクリプトでは判定せず、人手確認項目として明示する。音声内容には一切触れない（NFR-SEC-04）。

依存なし（標準ライブラリのみ）。実行:
`python tools/verify/analyze_recording.py <recordings/session または manifest.json>`
"""

from __future__ import annotations

import json
import sys
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Windows コンソール(cp932)で日本語/記号出力がクラッシュしないよう UTF-8 へ固定する。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

EXPECTED_SAMPLE_RATE = 16_000
EXPECTED_CHANNELS = 1
EXPECTED_BIT_DEPTH = 16
# 無音補填がこの比率を超えたら同期に難ありとして要確認に倒す（経験則・閾値は要調整）。
SILENCE_FILL_WARN_RATIO = 0.05
# meta と WAV 実測長の許容差（秒）。
DURATION_TOLERANCE_SEC = 0.5

# 検証1項目の結果: (マーク, 表示行)。マーク "" は情報行（判定に影響しない）。
CheckResult = tuple[str, str]


@dataclass(frozen=True)
class WavFacts:
    """WAV ヘッダから読んだ実測値（内容には触れない）。"""

    sample_rate: int
    channels: int
    bit_depth: int
    frames: int
    duration_sec: float
    valid: bool
    error: str | None = None


def read_wav_facts(path: Path) -> WavFacts:
    """WAV ヘッダのみ読む。壊れた/空のファイルは valid=False で返す（例外を握りつぶさず文脈付き）。"""
    try:
        if not path.exists():
            return WavFacts(0, 0, 0, 0, 0.0, valid=False, error=f"ファイルが存在しません: {path}")
        if path.stat().st_size == 0:
            return WavFacts(0, 0, 0, 0, 0.0, valid=False, error="ファイルが 0 バイト（ヘッダ未確定の疑い）")
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate()
            channels = w.getnchannels()
            bit_depth = w.getsampwidth() * 8
            frames = w.getnframes()
            duration = frames / rate if rate else 0.0
            return WavFacts(
                rate,
                channels,
                bit_depth,
                frames,
                duration,
                valid=frames > 0,
                error=None if frames > 0 else "data チャンクが空（録音が無効の疑い）",
            )
    except (wave.Error, EOFError, OSError) as exc:
        return WavFacts(0, 0, 0, 0, 0.0, valid=False, error=f"WAV ヘッダ解析失敗: {exc}")


def _resolve_manifest(arg: Path) -> Path:
    """引数がディレクトリなら manifest.json を補完する。"""
    if arg.is_dir():
        return arg / "manifest.json"
    return arg


def _check(label: str, ok: bool | None, detail: str) -> CheckResult:
    """(マーク, 行) を返す。ok=None は人手確認（▲）。"""
    mark = "OK " if ok else ("▲ " if ok is None else "NG ")
    return mark, f"  [{mark.strip()}] {label}: {detail}"


def _resolve_wav_path(stream: dict[str, Any], base: Path) -> Path:
    """stream の wavPath を解決する。相対/ファイル名のみなら manifest ディレクトリ基準で補完。"""
    wav_rel = stream.get("wavPath", "")
    wav_path = Path(wav_rel)
    if wav_path.is_absolute():
        return wav_path
    cand = base / Path(wav_rel).name
    return cand if cand.exists() else (base / wav_rel)


def _check_manifest_fields(manifest: dict[str, Any]) -> list[CheckResult]:
    """manifest レベルの検査（status / ストリーム数・役割 / commonStartUtc）。"""
    status = manifest.get("status")
    streams = manifest.get("streams", [])
    # streamRole 欠落（None）を含み得るため str 化してから並べる。None 混在で sorted が
    # TypeError になると、**壊れた manifest を診断するための本ツール自身**が落ちる。
    roles = sorted(str(s.get("streamRole")) for s in streams)
    common_start = manifest.get("commonStartUtc")
    return [
        _check("status", status == "complete", f"{status}（complete 期待。incomplete は部分録音）"),
        _check(
            "ストリーム数/役割",
            roles == ["others", "self"],
            f"{len(streams)}件 roles={roles}（self+others の2件期待・BR-IO-03）",
        ),
        _check("commonStartUtc", bool(common_start), f"{common_start}"),
    ]


def _check_stream(stream: dict[str, Any], base: Path) -> list[CheckResult]:
    """1 ストリームの WAV 有効性・形式・長さ整合・同期(無音補填)を検査する。"""
    role = stream.get("streamRole", "?")
    wav_path = _resolve_wav_path(stream, base)
    facts = read_wav_facts(wav_path)

    fmt_ok = (
        facts.valid
        and facts.sample_rate == EXPECTED_SAMPLE_RATE
        and facts.channels == EXPECTED_CHANNELS
        and facts.bit_depth == EXPECTED_BIT_DEPTH
    )
    meta_dur = float(stream.get("durationSec", 0.0))
    dur_ok = facts.valid and abs(meta_dur - facts.duration_sec) <= DURATION_TOLERANCE_SEC
    silence = float(stream.get("silenceFilledSec", 0.0))
    ratio = (silence / meta_dur) if meta_dur > 0 else 0.0

    return [
        ("", f"  --- stream [{role}] {wav_path.name} ---"),
        _check(f"[{role}] WAV 有効性", facts.valid, facts.error or f"frames={facts.frames}"),
        _check(
            f"[{role}] 形式 16k/mono/16bit",
            fmt_ok if facts.valid else False,
            f"{facts.sample_rate}Hz/{facts.channels}ch/{facts.bit_depth}bit",
        ),
        _check(
            f"[{role}] 長さ整合",
            dur_ok if facts.valid else False,
            f"meta={meta_dur:.2f}s / WAV={facts.duration_sec:.2f}s（許容±{DURATION_TOLERANCE_SEC}s）",
        ),
        _check(
            f"[{role}] 同期(無音補填)",
            ratio <= SILENCE_FILL_WARN_RATIO,
            f"補填 {silence:.3f}s（{ratio * 100:.1f}%, 警告閾値 {SILENCE_FILL_WARN_RATIO * 100:.0f}%・FR-04/SyncMath）",
        ),
    ]


def _print_report(manifest_path: Path, results: list[CheckResult]) -> None:
    """検査結果と人手確認項目を整形出力する（本文・音声内容には触れない）。"""
    print("=" * 64)
    print(f"SC-P1 録音/同期 検証 - {manifest_path}")
    print("=" * 64)
    print("\n".join(line for _, line in results))
    print("-" * 64)
    print("人手確認（内容/運用・本スクリプトでは判定不可）:")
    print("  [▲] 会議に参加せず self(マイク)/others(ループバック)を取得できたか")
    print("  [▲] 通知音・他アプリ音の混入が許容範囲か")
    print("  [▲] 同一デバイスをマイク/スピーカー兼用した場合の音響ブリード有無（運用知見・ISS-02）")
    print("-" * 64)


def analyze(manifest_path: Path) -> int:
    """SC-P1 機械判定。戻り値 0=PASS, 1=要確認/不合格（呼び出し側の終了コード）。"""
    if not manifest_path.exists():
        print(f"NG: manifest が見つかりません: {manifest_path}")
        print("    → ISS-14 の疑い（Ctrl+C による異常終了で manifest 未出力）。録音は無効。")
        return 1

    try:
        manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"NG: manifest 解析失敗: {exc}")
        return 1

    base = manifest_path.parent
    results = _check_manifest_fields(manifest)
    for stream in manifest.get("streams", []):
        results.extend(_check_stream(stream, base))

    _print_report(manifest_path, results)

    has_ng = any(mark.strip() == "NG" for mark, _ in results)
    verdict = "FAIL（NG あり）" if has_ng else "PASS（機械判定は合格・上記 ▲ を人手確認）"
    print(f"判定: {verdict}")
    return 1 if has_ng else 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print(__doc__)
        print("引数エラー: 録音セッションディレクトリまたは manifest.json のパスを1つ指定してください。")
        return 2
    return analyze(_resolve_manifest(Path(args[0])))


if __name__ == "__main__":
    sys.exit(main())
