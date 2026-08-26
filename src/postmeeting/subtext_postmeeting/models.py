"""ドメインエンティティ（domain-entities.md）。

技術非依存の値オブジェクト群。IO/状態を持つ取得層は含まず、データを受け取り結果を返す
純粋変換（FR-11）の対象となる型を中心に定義する。JSON 表現は camelCase・enum は文字列で、
Unit A の連携 JSON（RecordingModels.cs）と整合させる（BR-MERGE-04, FR-16）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# enum（JSON では小文字文字列。Unit A の JsonStringEnumConverter(camelCase) と一致）
# ---------------------------------------------------------------------------
class StreamRole(str, Enum):
    """音声系統。self=自分（分離なし固定話者）/ others=相手（話者分離あり）。"""

    SELF = "self"
    OTHERS = "others"


class InputMode(str, Enum):
    """入力モード。paired=2系統本番 / single=単一WAV検証（Q7=A, SC-P2先行検証）。"""

    PAIRED = "paired"
    SINGLE = "single"


# ---------------------------------------------------------------------------
# datetime ヘルパ — Unit A は ISO 8601(UTC) で出力する。揺れに耐えるパースを行う。
# ---------------------------------------------------------------------------
def parse_utc(value: str | None) -> datetime | None:
    """ISO 8601 文字列を UTC aware datetime に変換する。None は None のまま返す。"""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    # 末尾 'Z' を +00:00 に正規化（fromisoformat は Z を解さない版がある）。
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_utc(value: datetime | None) -> str | None:
    """UTC datetime を末尾 'Z' 付き ISO 8601 文字列に変換する。"""
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Unit A 連携契約（RecordingModels.cs と対応, FR-16）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SidecarMeta:
    """各 WAV に付随するメタ（連携契約）。Unit A の SidecarMeta に対応。"""

    wav_path: str
    stream_role: StreamRole
    start_time_utc: datetime
    sample_rate: int
    channels: int
    bit_depth: int
    device_name: str
    duration_sec: float
    silence_filled_sec: float

    @staticmethod
    def from_json(data: dict[str, Any]) -> "SidecarMeta":
        return SidecarMeta(
            wav_path=str(data["wavPath"]),
            stream_role=StreamRole(data["streamRole"]),
            start_time_utc=_require_utc(data.get("startTimeUtc"), "startTimeUtc"),
            sample_rate=int(data["sampleRate"]),
            channels=int(data["channels"]),
            bit_depth=int(data["bitDepth"]),
            device_name=str(data.get("deviceName", "")),
            duration_sec=float(data.get("durationSec", 0.0)),
            silence_filled_sec=float(data.get("silenceFilledSec", 0.0)),
        )


@dataclass(frozen=True)
class RecordingManifest:
    """録音セッションのマニフェスト（連携契約）。Unit B の入口（BR-IN-01/05）。"""

    session_id: str
    created_at_utc: datetime
    streams: tuple[SidecarMeta, ...]
    status: str  # "complete" | "incomplete"
    common_start_utc: datetime

    @property
    def is_incomplete(self) -> bool:
        return self.status.lower() == "incomplete"

    def stream(self, role: StreamRole) -> SidecarMeta | None:
        return next((s for s in self.streams if s.stream_role == role), None)

    @staticmethod
    def from_json(data: dict[str, Any]) -> "RecordingManifest":
        streams = tuple(SidecarMeta.from_json(s) for s in data.get("streams", []))
        return RecordingManifest(
            session_id=str(data["sessionId"]),
            created_at_utc=_require_utc(data.get("createdAtUtc"), "createdAtUtc"),
            streams=streams,
            status=str(data["status"]),
            common_start_utc=_require_utc(data.get("commonStartUtc"), "commonStartUtc"),
        )


# ---------------------------------------------------------------------------
# 入力解決結果（RecordingInput）— L1 / BR-IN
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RecordingInput:
    """入力解決結果。paired/single を同一型で表現し本番経路とコード共有（Q7=A）。"""

    mode: InputMode
    session_id: str
    others_wav_path: Path
    language: str
    common_start_utc: datetime | None = None  # paired のみ（統合の絶対時刻アンカー）
    self_wav_path: Path | None = None  # paired のみ
    is_partial: bool = False  # manifest.status=incomplete 由来（BR-IN-03）


# ---------------------------------------------------------------------------
# Transcribe ジョブ仕様（TranscribeJobSpec）— L3 / BR-JOB
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TranscribeJobSpec:
    """Transcribe バッチ起動パラメータ。系統ごとに1つ（Q1=A 別ジョブ）。"""

    job_name: str
    role: StreamRole
    media_s3_uri: str
    language_code: str
    show_speaker_labels: bool
    max_speaker_labels: int | None = None  # others のみ
    vocabulary_name: str | None = None  # C1・FR-C1-02。設定時のみ Settings.VocabularyName へ
    media_format: str = "wav"  # 録音経路は wav 固定。mp4→VTT ツールは ffmpeg 抽出後の "flac"（FR-18）


# ---------------------------------------------------------------------------
# パース結果（TranscriptSegment / RawTranscript）— L4 / BR-PARSE
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TranscriptSegment:
    """1発言セグメント（Q5=A セグメント粒度。単語TSは保持しない）。"""

    speaker_label: str  # self系統は "self" 固定 / others系統は "spk_0"..
    start_sec: float
    end_sec: float
    text: str
    confidence: float | None = None  # セグメント内 item 信頼度の平均（取得不能は None）


@dataclass(frozen=True)
class RawTranscript:
    """Transcribe 生結果のパース済み表現（系統別）。"""

    role: StreamRole
    session_id: str
    language: str
    segments: tuple[TranscriptSegment, ...]
    model_info: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 話者クラスタ / 実名マッピング（SpeakerCluster / SpeakerNameMap）— L6 / BR-NAME
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SpeakerCluster:
    """検出話者クラスタ（実名割当のための提示単位）。"""

    label: str
    sample_utterances: tuple[str, ...]
    segment_count: int
    total_sec: float

    def to_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "sampleUtterances": list(self.sample_utterances),
            "segmentCount": self.segment_count,
            "totalSec": round(self.total_sec, 3),
        }


@dataclass(frozen=True)
class MeetingInfo:
    """人手で記入する会議情報（会議名・日時・参加者。FR-MI-01）。

    いずれも任意。未記入（None／空）の項目は summarize 側が現行の決定的算出へフォールバックする
    （BR-MI-01・BR-SUM-09 改訂）。ここでの参加者は表示名（実名または話者ラベル）であり、
    `speaker_names.json` の実名解決とは別ファイルで持つ（BR-NAME-04 のログ非出力規則と同様、
    本体は PII を含み得るためログには出さない）。
    """

    title: str | None = None
    meeting_datetime: str | None = None
    participants: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "datetime": self.meeting_datetime,
            "participants": list(self.participants),
        }

    @staticmethod
    def from_json(data: dict[str, Any]) -> "MeetingInfo":
        title = data.get("title")
        meeting_datetime = data.get("datetime")
        participants = tuple(str(p).strip() for p in data.get("participants", []) if str(p).strip())
        return MeetingInfo(
            title=(str(title).strip() or None) if title else None,
            meeting_datetime=(str(meeting_datetime).strip() or None) if meeting_datetime else None,
            participants=participants,
        )

    def is_empty(self) -> bool:
        """3項目すべて未記入かどうか（純粋・summarize のフォールバック判定に使う）。"""
        return self.title is None and self.meeting_datetime is None and not self.participants


@dataclass(frozen=True)
class SpeakerNameMap:
    """spk_n→実名 のマッピング（FR-09・ファイル方式 Q3=A）。"""

    session_id: str
    mappings: dict[str, str]
    unresolved: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "mappings": dict(self.mappings),
            "unresolved": list(self.unresolved),
        }

    @staticmethod
    def from_json(data: dict[str, Any]) -> "SpeakerNameMap":
        return SpeakerNameMap(
            session_id=str(data.get("sessionId", "")),
            mappings={str(k): str(v) for k, v in dict(data.get("mappings", {})).items()},
            unresolved=tuple(str(u) for u in data.get("unresolved", [])),
        )


# ---------------------------------------------------------------------------
# 最終トランスクリプト（ResolvedSegment / FinalTranscript）— 出力契約 FR-16
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ResolvedSegment:
    """統合済みセグメント（TranscriptSegment + 統合情報）。"""

    speaker: str  # 実名（割当済）または spk_n / self（未割当）
    origin: StreamRole
    start_sec: float
    end_sec: float
    text: str
    confidence: float | None = None
    absolute_start_utc: datetime | None = None  # paired のみ

    def to_json(self) -> dict[str, Any]:
        return {
            "speaker": self.speaker,
            "origin": self.origin.value,
            "startSec": round(self.start_sec, 3),
            "endSec": round(self.end_sec, 3),
            "absoluteStartUtc": format_utc(self.absolute_start_utc),
            "text": self.text,
            "confidence": self.confidence,
        }

    @staticmethod
    def from_json(data: dict[str, Any]) -> "ResolvedSegment":
        return ResolvedSegment(
            speaker=str(data["speaker"]),
            origin=StreamRole(data["origin"]),
            start_sec=float(data["startSec"]),
            end_sec=float(data["endSec"]),
            text=str(data.get("text", "")),
            confidence=_opt_float(data.get("confidence")),
            absolute_start_utc=parse_utc(data.get("absoluteStartUtc")),
        )


@dataclass(frozen=True)
class FinalTranscript:
    """2系統統合済みの最終トランスクリプト（FR-16 連携点）。下流の唯一の入力（FR-11）。"""

    session_id: str
    language: str
    segments: tuple[ResolvedSegment, ...]
    speakers: tuple[str, ...]
    common_start_utc: datetime | None = None
    model_info: dict[str, Any] = field(default_factory=dict)
    is_partial: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "language": self.language,
            "commonStartUtc": format_utc(self.common_start_utc),
            "segments": [s.to_json() for s in self.segments],
            "speakers": list(self.speakers),
            "modelInfo": dict(self.model_info),
            "isPartial": self.is_partial,
        }

    @staticmethod
    def from_json(data: dict[str, Any]) -> "FinalTranscript":
        return FinalTranscript(
            session_id=str(data["sessionId"]),
            language=str(data.get("language", "ja-JP")),
            segments=tuple(ResolvedSegment.from_json(s) for s in data.get("segments", [])),
            speakers=tuple(str(s) for s in data.get("speakers", [])),
            common_start_utc=parse_utc(data.get("commonStartUtc")),
            model_info=dict(data.get("modelInfo", {})),
            is_partial=bool(data.get("isPartial", False)),
        )


# ---------------------------------------------------------------------------
# 議事録（MinutesDoc）— L7 / FR-10
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MinutesDoc:
    """Bedrock 生成の構造化 Markdown 議事録。"""

    session_id: str
    markdown: str
    source_model: str
    generated_at_utc: datetime

    def to_json(self) -> dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "sourceModel": self.source_model,
            "generatedAtUtc": format_utc(self.generated_at_utc),
        }


# ---------------------------------------------------------------------------
# パイプライン段階（Stage）— Q4=A 中間成果物再利用（完了段は成果物ファイルの有無で判定する）
# ---------------------------------------------------------------------------
class Stage(str, Enum):
    """パイプライン段階。順序は値の昇順と一致する。"""

    TRANSCRIBED = "transcribed"
    MERGED = "merged"
    NAMED = "named"
    CORRECTED = "corrected"  # C2: LLM 後処理補正（固有名詞表記是正）。NAMED→SUMMARIZED 間（FR-C2-01）
    SUMMARIZED = "summarized"


# ---------------------------------------------------------------------------
# 内部ヘルパ
# ---------------------------------------------------------------------------
def _require_utc(value: Any, field_name: str) -> datetime:
    parsed = parse_utc(value if isinstance(value, str) else None)
    if parsed is None:
        raise ValueError(f"必須の日時フィールド '{field_name}' が不正または欠落しています。")
    return parsed


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)
