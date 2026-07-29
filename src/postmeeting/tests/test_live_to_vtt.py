"""B1F: ライブ字幕 JSONL→VTT 変換の単体テスト（純粋・AWS 不要）。"""

from __future__ import annotations

import json

from subtext_postmeeting.live_to_vtt import jsonl_to_vtt
from subtext_postmeeting.vtt_parser import parse_vtt


def _line(offset_ms: int, speaker: str, text: str, source: str = "others") -> str:
    return json.dumps(
        {"offsetMs": offset_ms, "source": source, "speaker": speaker, "text": text, "captureUtc": None},
        ensure_ascii=False,
    )


def _line_utc(offset_ms: int, capture_utc: str | None, speaker: str, text: str, source: str = "others") -> str:
    """captureUtc 付きの1行（A1 再接続では offsetMs が 0 起点へ戻る）。"""
    return json.dumps(
        {"offsetMs": offset_ms, "source": source, "speaker": speaker, "text": text, "captureUtc": capture_utc},
        ensure_ascii=False,
    )


def test_basic_conversion_emits_voice_tags() -> None:
    lines = [
        _line(1000, "自分", "おはよう", "self"),
        _line(4000, "相手", "こんにちは", "others"),
    ]
    vtt = jsonl_to_vtt(lines)
    assert vtt.startswith("WEBVTT\n")
    assert "<v 自分>おはよう</v>" in vtt
    assert "<v 相手>こんにちは</v>" in vtt
    # 開始時刻は offset から厳密（1.0s / 4.0s）。
    assert "00:00:01.000 --> 00:00:04.000" in vtt


def test_records_sorted_by_offset() -> None:
    lines = [_line(5000, "相手", "後"), _line(1000, "自分", "先", "self")]
    vtt = jsonl_to_vtt(lines)
    assert vtt.index("先") < vtt.index("後")


def test_broken_and_empty_lines_skipped() -> None:
    lines = ["", "これはJSONではない", _line(1000, "自分", "有効", "self"), "{壊れた"]
    vtt = jsonl_to_vtt(lines)
    assert "<v 自分>有効</v>" in vtt
    # 有効 cue は1つだけ（cue 番号 1 のみ）。
    assert "\n1\n" in vtt
    assert "\n2\n" not in vtt


def test_valid_json_without_offset_is_skipped() -> None:
    # JSON として妥当でも offsetMs を欠く行・非 dict 行は cue にしない（追記中の部分行に耐える）。
    lines = [
        json.dumps({"speaker": "自分", "text": "offset なし"}, ensure_ascii=False),
        "123",  # 妥当な JSON だが dict でない
        _line(1000, "自分", "有効", "self"),
    ]
    vtt = jsonl_to_vtt(lines)
    assert "<v 自分>有効</v>" in vtt
    assert "offset なし" not in vtt
    assert "\n2\n" not in vtt  # 有効 cue は1つだけ


def test_last_cue_uses_padding_end() -> None:
    vtt = jsonl_to_vtt([_line(2000, "自分", "最後", "self")])
    # 最終 cue: 2.0s → 2.0s + 3.0s = 5.0s。
    assert "00:00:02.000 --> 00:00:05.000" in vtt


def test_special_characters_escaped() -> None:
    vtt = jsonl_to_vtt([_line(0, "自分", "a<b>&c", "self")])
    assert "a&lt;b&gt;&amp;c" in vtt


def test_empty_input_yields_header_only() -> None:
    assert jsonl_to_vtt([]).strip() == "WEBVTT"


def test_reconnect_order_follows_capture_utc_not_offset() -> None:
    # A1 の再接続で offsetMs は新セッションの 0 起点へ戻る。captureUtc を正とすれば
    # 再接続後の cue が前方へ回り込まない（BR-B1F-TIME-01）。
    lines = [
        _line_utc(10_000, "2026-07-29T10:00:10.000Z", "相手", "再接続の前", "others"),
        _line_utc(500, "2026-07-29T10:00:20.000Z", "自分", "再接続の後", "self"),
    ]
    vtt = jsonl_to_vtt(lines)
    assert vtt.index("再接続の前") < vtt.index("再接続の後")


def test_capture_utc_defines_start_times_from_earliest() -> None:
    # 最小の captureUtc を 0 起点とし、実経過時間で cue 開始を置く。
    lines = [
        _line_utc(10_000, "2026-07-29T10:00:10.000Z", "相手", "前", "others"),
        _line_utc(500, "2026-07-29T10:00:20.500Z", "自分", "後", "self"),
    ]
    vtt = jsonl_to_vtt(lines)
    assert "00:00:00.000 --> 00:00:10.500" in vtt
    assert "00:00:10.500 --> 00:00:13.500" in vtt


def test_falls_back_to_offset_when_capture_utc_missing_on_any_record() -> None:
    # 1行でも captureUtc を欠くと壁時計の通し時刻を作れないため offsetMs 昇順に倒す。
    lines = [
        _line_utc(5000, "2026-07-29T10:00:05.000Z", "相手", "後", "others"),
        _line_utc(1000, None, "自分", "先", "self"),
    ]
    vtt = jsonl_to_vtt(lines)
    assert vtt.index("先") < vtt.index("後")


def test_malformed_capture_utc_is_treated_as_missing() -> None:
    lines = [
        _line_utc(5000, "not-a-timestamp", "相手", "後", "others"),
        _line_utc(1000, "2026-07-29T10:00:01.000Z", "自分", "先", "self"),
    ]
    vtt = jsonl_to_vtt(lines)
    assert vtt.index("先") < vtt.index("後")


def test_naive_capture_utc_is_treated_as_utc() -> None:
    # Z 表記を欠く値でも UTC とみなし、aware 値との減算で落ちない。
    lines = [
        _line_utc(0, "2026-07-29T10:00:00.000", "自分", "先", "self"),
        _line_utc(0, "2026-07-29T10:00:02.000Z", "相手", "後", "others"),
    ]
    vtt = jsonl_to_vtt(lines)
    assert vtt.index("先") < vtt.index("後")
    assert "00:00:00.000 --> 00:00:02.000" in vtt


def test_output_is_parseable_by_vtt_parser() -> None:
    # 生成 VTT が既存 vtt_parser（--mode vtt 経路）で読め、話者名が初期値ヒントに入る。
    lines = [_line(1000, "自分", "決定事項です", "self"), _line(3000, "相手", "了解", "others")]
    ft = parse_vtt(jsonl_to_vtt(lines), "sess", "ja-JP")
    speakers = {seg.speaker for seg in ft.segments}
    assert speakers == {"spk_0", "spk_1"}
    # <v 自分>/<v 相手> は実名ヒント（機械ラベルでない）として命名ゲート初期値に入る。
    assert ft.model_info["speakerNameHints"]["spk_0"] == "自分"
