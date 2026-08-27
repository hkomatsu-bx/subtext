"""ライブ字幕 JSONL → WebVTT 変換（B1F・FR-B1F-05）【第3 CLI】。

Unit C が追記したライブ字幕 JSONL（`{offsetMs, source, speaker, text, captureUtc}`・
確定 final のみ）を WebVTT（`<v 話者ラベル>` 書式）へ変換する。出力 VTT は既存の
`subtext-postmeeting --mode vtt` にそのまま渡せ、命名ゲート（実名化）＋C2＋要約へ合流する。

cue 開始時刻は `captureUtc`（壁時計）を正とする。A1 の再接続では `offsetMs` が新しい
ストリーミングセッションの 0 起点へ戻るため、offsetMs だけで並べると再接続後の cue が
再接続前より前へ回り込む（BR-B1F-TIME-01）。cue 終了時刻は JSONL に無いため変換時に
導出する（次 cue の開始／最終 cue は既定パディング）。整形とエスケープは既存
`vtt_writer` を再利用する（重複実装しない）。音声内容はログに出さない（BR-ERR-04 と同旨）。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .vtt_writer import _escape, _fmt_timestamp

# 最終 cue の表示長（次 cue が無いため固定パディング）。
_LAST_CUE_PADDING_MS = 3000


@dataclass(frozen=True)
class _Caption:
    """ライブ字幕 JSONL の1確定 cue（型付け済み）。

    offset_ms はストリーミングセッション相対のため、再接続をまたぐと巻き戻る。
    時系列の正は capture_utc（壁時計。Unit C が算出できなかったときのみ None）。
    """

    offset_ms: int
    speaker: str
    text: str
    capture_utc: datetime | None


def jsonl_to_vtt(lines: Iterable[str]) -> str:
    """ライブ字幕 JSONL 行群を WebVTT 文字列へ変換する（純粋・NFR-B1F-01）。

    - 開始時刻は `captureUtc` 基準で決め、その昇順に安定ソートする（欠けるときは `offsetMs` 基準）。
    - 各 cue の終了は次 cue の開始（同時刻以下なら +1ms で単調増加を保証）。最終 cue は開始 + パディング。
    - JSON パース不能な行・必須キー欠落行は無視する（追記中の未完行や破損に耐える）。
    """
    timeline = _ordered_starts(_parse_records(lines))
    out: list[str] = ["WEBVTT", ""]
    total = len(timeline)
    for index, (start_ms, cap) in enumerate(timeline):
        if index + 1 < total:
            next_ms = timeline[index + 1][0]
            end_ms = next_ms if next_ms > start_ms else start_ms + 1
        else:
            end_ms = start_ms + _LAST_CUE_PADDING_MS

        out.append(str(index + 1))
        out.append(f"{_fmt_timestamp(start_ms / 1000)} --> {_fmt_timestamp(end_ms / 1000)}")
        out.append(f"<v {cap.speaker}>{_escape(cap.text)}</v>")
        out.append("")

    return "\n".join(out).rstrip("\n") + "\n"


def _parse_records(lines: Iterable[str]) -> list[_Caption]:
    """JSONL 行を _Caption へパースし、追記順のまま返す（破損行・offsetMs 欠落行は無視）。"""
    captions: list[_Caption] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue  # 追記中の未完行・破損行は捨てる（BR-B1F-IO-01）
        caption = _to_caption(obj)
        if caption is not None:
            captions.append(caption)
    return captions


def _ordered_starts(captions: list[_Caption]) -> list[tuple[int, _Caption]]:
    """各 cue の開始 ms を確定し、時系列の昇順に並べて返す（BR-B1F-TIME-01）。

    `captureUtc` が全 cue に揃っていれば、その最小値を 0 として経過 ms を算出する。
    A1 の再接続で `offsetMs` が 0 起点へ戻っても、壁時計基準なら順序と間隔を保てる。
    揃っていない場合は `offsetMs` 昇順に倒す。この経路では、再接続をまたいだ順序は
    保証できない（Unit C が captureUtc を算出できたときのみ正確になる）。
    """
    stamps = [cap.capture_utc for cap in captions]
    if captions and all(stamp is not None for stamp in stamps):
        base = min(stamp for stamp in stamps if stamp is not None)
        # strict=True: stamps は captions から 1:1 で作るため長さは必ず一致する（崩れたら即座に露見させる）。
        pairs = [
            (_elapsed_ms(base, stamp), cap)
            for stamp, cap in zip(stamps, captions, strict=True)
            if stamp is not None
        ]
    else:
        pairs = [(cap.offset_ms, cap) for cap in captions]
    pairs.sort(key=lambda pair: pair[0])
    return pairs


def _elapsed_ms(base: datetime, stamp: datetime) -> int:
    """base からの経過ミリ秒（負にはならない。base は最小値のため）。"""
    return round((stamp - base).total_seconds() * 1000)


def _to_caption(obj: object) -> _Caption | None:
    """パース済み JSON 値を _Caption へ変換する（dict でない/offsetMs 欠落は None）。

    speaker は speaker→source→"spk_0" の順にフォールバックする（元実装と同じ or 連鎖）。
    """
    if not isinstance(obj, dict) or "offsetMs" not in obj:
        return None
    speaker = str(obj.get("speaker") or obj.get("source") or "spk_0")
    return _Caption(
        offset_ms=int(obj["offsetMs"]),
        speaker=speaker,
        text=str(obj.get("text", "")),
        capture_utc=_parse_capture_utc(obj.get("captureUtc")),
    )


def _parse_capture_utc(value: object) -> datetime | None:
    """`captureUtc`（`yyyy-MM-ddTHH:mm:ss.fffZ`）を aware な datetime へ。不正値は None。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # タイムゾーン表記を欠く値は UTC とみなす（Unit C は常に Z 付きで書く）。
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="subtext-live-to-vtt",
        description="ライブ字幕 JSONL を WebVTT へ変換する（B1F）。出力は --mode vtt に渡せる。",
    )
    parser.add_argument("jsonl", type=Path, help="入力: ライブ字幕 JSONL（Unit C が追記したもの）")
    parser.add_argument("--out", type=Path, help="出力 VTT パス（既定: 入力と同じ場所の .vtt 拡張子）")
    parser.add_argument(
        "--force", action="store_true", help="出力 VTT が既にあっても上書きする（手編集は失われる）"
    )
    args = parser.parse_args(argv)

    if not args.jsonl.is_file():
        print(f"JSONL が見つかりません: {args.jsonl}", file=sys.stderr)
        return 1

    try:
        lines = args.jsonl.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"JSONL を読み込めません: {args.jsonl}（{exc.strerror}）", file=sys.stderr)
        return 1

    vtt = jsonl_to_vtt(lines)
    out_path = args.out if args.out is not None else args.jsonl.with_suffix(".vtt")
    if out_path.exists() and not args.force:
        # 既存 VTT を黙って壊さない（mp4→VTT と同じ扱い）。1 回目の出力に話者名を手で入れて
        # あった場合、2 回目の実行でその編集が消える。
        print(
            f"出力 VTT が既に存在します: {out_path}\n"
            "  手で編集した内容が失われます。`--out` で別の出力先を指定するか、"
            "意図した上書きなら `--force` を付けてください。",
            file=sys.stderr,
        )
        return 1
    try:
        out_path.write_text(vtt, encoding="utf-8")
    except OSError as exc:
        print(f"VTT を書き込めません: {out_path}（{exc.strerror}）", file=sys.stderr)
        return 1

    print(f"VTT を生成しました: {out_path}")
    return 0
