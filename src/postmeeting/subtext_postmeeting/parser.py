"""Transcribe 結果パース（L4 / BR-PARSE）【純粋関数】。

Transcribe バッチ出力 JSON（dict）を受け取り RawTranscript を返す。外部状態を読まない純粋
変換（FR-11）。出力粒度は発言セグメント単位で、単語タイムスタンプは保持しない（BR-PARSE-01）。
confidence はセグメント内 pronunciation item 信頼度の平均（取得不能は None, BR-PARSE-02）。
others は speaker_labels の話者境界で分割し、self は休止で分割し話者 `self` 固定（BR-PARSE-03）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import RawTranscript, StreamRole, TranscriptSegment

# self 系統セグメント分割の休止しきい値（秒）。これを超える無音で区切る。
_PAUSE_GAP_SEC = 1.0
_SELF_LABEL = "self"
# 語を空白で区切らない言語（分かち書きをしない）。日本語・中国語がこれに当たる。
# これ以外の言語（`LANGUAGE=en-US` 等）で無空白連結すると `helloworld` のように語が
# 潰れ、議事録も検索も読めなくなる。
_UNSPACED_LANGUAGE_PREFIXES = ("ja", "zh", "yue")


@dataclass(frozen=True)
class _Token:
    """results.items の1要素。句読点は start/end を持たない。"""

    is_pronunciation: bool
    content: str
    start_sec: float | None
    end_sec: float | None
    confidence: float | None
    start_raw: str | None  # speaker_labels との突合用（元の文字列表現を保持）


def parse(raw_result: dict[str, Any], role: StreamRole, session_id: str, language: str) -> RawTranscript:
    """Transcribe 出力を RawTranscript に変換する。"""
    results = raw_result.get("results", {})
    tokens = _read_tokens(results.get("items", []))
    speaker_by_start = {} if role == StreamRole.SELF else _speaker_index(results.get("speaker_labels", {}))

    labeled = _assign_speakers(tokens, role, speaker_by_start)
    segments = _build_segments(labeled, word_separator=_word_separator(language))
    model_info = {
        "jobName": raw_result.get("jobName"),
        "language": language,
        "role": role.value,
    }
    return RawTranscript(
        role=role,
        session_id=session_id,
        language=language,
        segments=tuple(segments),
        model_info={k: v for k, v in model_info.items() if v is not None},
    )


def _read_tokens(items: list[dict[str, Any]]) -> list[_Token]:
    """results.items を _Token 列へ変換する（pronunciation/punctuation を判別）。"""
    return [_read_token(item) for item in items]


def _read_token(item: dict[str, Any]) -> _Token:
    """results.items の1要素を _Token へ変換する（句読点は時刻・信頼度を持たない）。"""
    first = (item.get("alternatives") or [{}])[0]
    content = str(first.get("content", ""))
    if item.get("type") != "pronunciation":
        # punctuation 等は直前の発話に連結する（時刻・信頼度なし）。
        return _Token(
            is_pronunciation=False,
            content=content,
            start_sec=None,
            end_sec=None,
            confidence=None,
            start_raw=None,
        )
    start_raw = item.get("start_time")
    return _Token(
        is_pronunciation=True,
        content=content,
        start_sec=_to_float(start_raw),
        end_sec=_to_float(item.get("end_time")),
        confidence=_to_float(first.get("confidence")),
        start_raw=str(start_raw) if start_raw is not None else None,
    )


def _speaker_index(speaker_labels: dict[str, Any]) -> dict[str, str]:
    """pronunciation の start_time(文字列) → 話者ラベル の対応を作る。

    後勝ち（同一 start は後のセグメントが上書き）は元実装と同じ。
    """
    index: dict[str, str] = {}
    for segment in speaker_labels.get("segments", []):
        index.update(_segment_speaker_starts(segment))
    return index


def _segment_speaker_starts(segment: dict[str, Any]) -> dict[str, str]:
    """1 セグメント内の start_time(文字列) → 話者ラベル を返す（話者ラベル空なら空 dict）。"""
    label = str(segment.get("speaker_label", ""))
    if not label:
        return {}
    return {str(start): label for item in segment.get("items", []) if (start := item.get("start_time")) is not None}


def _assign_speakers(
    tokens: list[_Token], role: StreamRole, speaker_by_start: dict[str, str]
) -> list[tuple[str, _Token]]:
    """各トークンに話者ラベルを割り当てる。句読点は直前話者を継承する。"""
    labeled: list[tuple[str, _Token]] = []
    last_speaker = _SELF_LABEL if role == StreamRole.SELF else "spk_0"
    for token in tokens:
        if role == StreamRole.SELF:
            speaker = _SELF_LABEL
        elif token.is_pronunciation and token.start_raw is not None:
            speaker = speaker_by_start.get(token.start_raw, last_speaker)
        else:
            speaker = last_speaker
        last_speaker = speaker
        labeled.append((speaker, token))
    return labeled


def _word_separator(language: str) -> str:
    """語をつなぐ区切り（純粋）。分かち書きしない言語は空文字、それ以外は空白。

    既定の `ja-JP` は空文字で従来どおり。`LANGUAGE` は設定で変えられるため（FR-15）、
    英語等を指定したときに語が潰れないようにする。
    """
    code = language.strip().lower()
    return "" if any(code.startswith(prefix) for prefix in _UNSPACED_LANGUAGE_PREFIXES) else " "


def _build_segments(labeled: list[tuple[str, _Token]], *, word_separator: str = "") -> list[TranscriptSegment]:
    """話者の切替・休止でセグメント化する。

    `word_separator` は発話語どうしをつなぐ文字（分かち書き言語では空白）。句読点は直前の語へ
    区切りなしで連結する（`hello world .` にならないように）。
    """
    segments: list[TranscriptSegment] = []
    cur_speaker: str | None = None
    texts: list[str] = []
    confidences: list[float] = []
    seg_start: float | None = None
    seg_end: float | None = None
    prev_end: float | None = None

    def flush() -> None:
        nonlocal cur_speaker, texts, confidences, seg_start, seg_end
        if cur_speaker is None or seg_start is None or seg_end is None:
            return
        text = "".join(texts).strip()
        if text:
            confidence = sum(confidences) / len(confidences) if confidences else None
            segments.append(
                TranscriptSegment(
                    speaker_label=cur_speaker,
                    start_sec=seg_start,
                    end_sec=seg_end,
                    text=text,
                    confidence=confidence,
                )
            )
        texts, confidences = [], []
        seg_start = seg_end = None

    for speaker, token in labeled:
        if not token.is_pronunciation:
            # 句読点は現セグメントへ連結（話者未確定ならスキップ）。ガード節で発話処理を1段浅くする。
            if cur_speaker is not None:
                texts.append(token.content)
            continue

        gap = token.start_sec - prev_end if (prev_end is not None and token.start_sec is not None) else 0.0
        speaker_changed = cur_speaker is not None and speaker != cur_speaker
        if speaker_changed or (cur_speaker is not None and gap > _PAUSE_GAP_SEC):
            flush()
        if seg_start is None:
            seg_start = token.start_sec
        cur_speaker = speaker
        seg_end = token.end_sec if token.end_sec is not None else seg_end
        if texts and word_separator:
            texts.append(word_separator)  # 分かち書き言語のみ語間に区切りを入れる
        texts.append(token.content)
        if token.confidence is not None:
            confidences.append(token.confidence)
        prev_end = token.end_sec if token.end_sec is not None else prev_end

    flush()
    return segments


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
