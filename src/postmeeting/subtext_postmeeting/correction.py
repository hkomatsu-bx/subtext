"""LLM 後処理補正（L6.5 / C2・FR-C2・BR-CORR）。

ASR 出力の固有名詞の表記を、用語リスト準拠で是正する。C1（Custom Vocabulary）の構造的限界
（ja-JP はローマ字化する英語固有名詞を救えない）を後処理で超える。

二段構成（BR-CORR / DG-C2-6）:
  1. 辞書段（決定的・非課金）— エイリアス→正規表記の置換。語境界規則で過剰一致を防ぐ。
  2. LLM 段（Bedrock・1 回）— 辞書で拾えない文脈依存の誤認識を、用語リストにある語に限り訂正。

設計上の不変条件（BR-CORR-01/02）: 補正はセグメントの `text` のみを変える。数・順序・話者・
時刻は不変。リストにない語・文構造・句読点は変えない。LLM 応答が崩れたら不採用に倒す
（辞書適用済みを採用）＝フェイルセーフ（NFR-C2-03）。本文 PII はログに出さない（NFR-C2-02）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from .aws import build_client
from .config import PipelineConfig
from .errors import PipelineError
from .models import FinalTranscript, ResolvedSegment

logger = logging.getLogger("subtext.postmeeting")

_STAGE = "correct"
_MAX_TOKENS = 4096
_ANTHROPIC_VERSION = "bedrock-2023-05-31"
# 非 ASCII エイリアスの最小文字数。和文は語境界の概念が無く素朴な部分一致になるため、短い
# エイリアスは無関係な語の内側に当たる（例: 「イジ」は「エンゲイジメント」「ペイジ」「デイジー」に
# 一致して本文を壊す）。辞書段は決定的・非課金で既定経路では必ず通り、結果は final_transcript.json →
# 議事録 → Slack までそのまま流れるため、設定として受け付けない（BR-CORR-06）。
_MIN_NON_ASCII_ALIAS_LEN = 3

_INSTRUCTION = """あなたは会議文字起こしの校正者です。以下の番号付き行それぞれについて、\
「用語リスト」にある固有名詞の誤認識だけを正しい表記へ訂正してください。

絶対的な制約:
- 用語リストにある語の誤認識のみを訂正する。リストにない語は一切変更しない。
- 文構造・語順・助詞・句読点・言い回しを変えない。要約・言い換え・補完・翻訳をしない。
- 行番号（id）はそのまま保持する。行を増減しない。
- 訂正した行だけを返す。訂正不要な行は返さない。

出力は次の JSON のみ（前後に説明文を付けない）:
{"corrections": [{"id": <整数>, "text": "<訂正後テキスト>"}]}
訂正が一つも無ければ {"corrections": []} を返す。"""


# ---------------------------------------------------------------------------
# 値オブジェクト
# ---------------------------------------------------------------------------
class CorrectionStatus(str, Enum):
    SKIPPED = "skipped"  # 用語リスト空/無効化（no-op）
    APPLIED = "applied"  # 辞書＋LLM が成功
    DICTIONARY_ONLY = "dictionary_only"  # LLM 失敗だが辞書分は適用済み
    FALLBACK = "fallback"  # LLM 失敗かつ辞書ヒット無し（補正前のまま）


@dataclass(frozen=True)
class CorrectionTerm:
    """正規表記と、その既知の誤認識エイリアス（DG-C2-5）。"""

    canonical: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class CorrectionOutcome:
    """補正結果。`transcript` は補正後の FinalTranscript。"""

    transcript: FinalTranscript
    status: CorrectionStatus
    dictionary_hits: int = 0
    llm_changed: int = 0
    model: str | None = None

    def meta(self) -> dict[str, Any]:
        """modelInfo.correction に格納する監査メタ（本文 PII は含めない, NFR-C2-02）。"""
        return {
            "applied": self.status in (CorrectionStatus.APPLIED, CorrectionStatus.DICTIONARY_ONLY),
            "status": self.status.value,
            "model": self.model,
            "dictionaryHits": self.dictionary_hits,
            "llmChanged": self.llm_changed,
        }


# ---------------------------------------------------------------------------
# 用語ファイル読込（FR-C2-06）
# ---------------------------------------------------------------------------
def load_terms(path: Path) -> tuple[CorrectionTerm, ...]:
    """用語ファイル（JSON）を読み込む。不在/空/`terms` 空は空タプル（no-op）。

    不正 JSON・スキーマ不正は握り潰さず PipelineError（設定ミスの早期顕在化, NFR-C2-03）。
    """
    if not path.is_file():
        logger.info("補正用語ファイルが無いため補正をスキップします: %s", path)
        return ()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"補正用語ファイルの読込に失敗しました: {path}", failed_stage=_STAGE) from exc

    raw_terms = data.get("terms") if isinstance(data, dict) else None
    if not raw_terms:
        return ()

    terms: list[CorrectionTerm] = []
    seen_alias: dict[str, str] = {}
    for entry in raw_terms:
        canonical = str(entry.get("canonical", "")).strip()
        if not canonical:
            raise PipelineError("補正用語ファイルに canonical 空のエントリがあります。", failed_stage=_STAGE)
        aliases = tuple(_read_aliases(entry, canonical))
        _register_aliases(aliases, canonical, seen_alias)
        terms.append(CorrectionTerm(canonical=canonical, aliases=aliases))
    return tuple(terms)


def _read_aliases(entry: dict[str, Any], canonical: str) -> list[str]:
    """1エントリの `aliases` を検証して読む（設定ミスの早期顕在化, NFR-C2-03）。

    文字列で書かれていた場合（`"aliases": "bx"`）は配列として反復すると **1 文字ずつの
    エイリアス**（`b`, `x`）に展開され、語境界付きの ASCII 一致で無関係な語まで書き換えてしまう。
    黙って通さず、書き方を示して停止する。
    """
    raw = entry.get("aliases", [])
    if isinstance(raw, str):
        raise PipelineError(
            f"補正用語 '{canonical}' の aliases は配列で指定してください"
            f'（例: "aliases": ["{raw}"]）。文字列だと 1 文字ずつのエイリアスに展開され、'
            "無関係な語を書き換えます。",
            failed_stage=_STAGE,
        )
    if not isinstance(raw, list):
        raise PipelineError(
            f"補正用語 '{canonical}' の aliases は配列で指定してください。", failed_stage=_STAGE
        )
    aliases = [a.strip() for a in raw if isinstance(a, str) and a.strip()]
    for alias in aliases:
        if not alias.isascii() and len(alias) < _MIN_NON_ASCII_ALIAS_LEN:
            raise PipelineError(
                f"補正用語 '{canonical}' の和文エイリアス '{alias}' が短すぎます"
                f"（{_MIN_NON_ASCII_ALIAS_LEN} 文字以上必要）。和文は語境界を判定できず部分一致するため、"
                "無関係な語の内側に当たって本文を書き換えます。",
                failed_stage=_STAGE,
            )
    return aliases


def _register_aliases(aliases: tuple[str, ...], canonical: str, seen_alias: dict[str, str]) -> None:
    """エイリアス→canonical を登録する。衝突（別 canonical に既割当）は警告し先勝ちで継続。"""
    for alias in aliases:
        key = alias.lower()
        if key in seen_alias and seen_alias[key] != canonical:
            logger.warning(
                "補正エイリアスの衝突: '%s' は '%s' に既割当（'%s' は無視）",
                alias,
                seen_alias[key],
                canonical,
            )
        else:
            seen_alias[key] = canonical


# ---------------------------------------------------------------------------
# 辞書段（決定的・C3・BR-CORR-06）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Dictionary:
    """エイリアス→正規表記の一括置換器（1 セグメント 1 パス）。

    エイリアスごとに `subn` を回すと、セグメント数×ルール数の走査になるだけでなく、**前の置換で
    生まれた正規表記に後のルールが当たる**（`BeeX` の中の `Bee` 等）。単一の交替パターンで
    1 パスにすると、置換結果は二度と走査されず、同じ位置では最長のエイリアスだけが効く
    （Python の交替は左優先なので、長い順に並べれば最長一致になる）。
    """

    pattern: re.Pattern[str] | None
    canonical_by_alias: Mapping[str, str]

    def substitute(self, text: str) -> tuple[str, int]:
        """1 パスで置換し、(結果, 置換数) を返す（純粋）。"""
        if self.pattern is None:
            return text, 0
        # 置換は関数で返す。文字列を渡すと `re.sub` の**テンプレート**として解釈され、正規表記の
        # `\1` が後方参照になり、末尾の単独 `\` は re.PatternError で落ちる（用語ファイルは人が
        # 書くため Windows パスのような表記が混じり得る）。関数なら常にリテラルとして入る。
        return self.pattern.subn(lambda m: self.canonical_by_alias[m.group(0).casefold()], text)


def _build_dictionary(terms: tuple[CorrectionTerm, ...]) -> _Dictionary:
    """エイリアス→正規表記の一括置換器を構築する。長いエイリアスを優先（最長一致）。

    ASCII エイリアス（bx, 1g 等）は語境界付きで大小無視マッチし、部分文字列誤爆を防ぐ。
    和文エイリアス（カタカナ等）は語境界の概念が無いため素朴一致だが、長い順の交替で過剰一致を抑える。
    """
    pairs = [(alias, term.canonical) for term in terms for alias in term.aliases]
    pairs.sort(key=lambda p: len(p[0]), reverse=True)  # 最長一致優先（交替は左優先）

    lookup: dict[str, str] = {}
    parts: list[str] = []
    for alias, canonical in pairs:
        key = alias.casefold()
        if key in lookup:
            continue  # 衝突は load_terms が警告済み。先勝ちで揃える。
        lookup[key] = canonical
        parts.append(_alias_pattern(alias))
    pattern = re.compile("|".join(parts)) if parts else None
    return _Dictionary(pattern=pattern, canonical_by_alias=lookup)


def _alias_pattern(alias: str) -> str:
    """エイリアス1件を交替パターンの1要素へ変換する（ASCII は語境界付き・大小無視）。

    大小無視はインラインフラグ `(?i:...)` で要素ごとに掛ける。パターン全体に `re.IGNORECASE` を
    付けると和文エイリアスの厳密一致まで緩むため、1 つの正規表現に両方を共存させる。
    """
    escaped = re.escape(alias)
    if alias.isascii():
        return rf"(?<![A-Za-z0-9])(?i:{escaped})(?![A-Za-z0-9])"
    return escaped


def apply_dictionary(
    segments: tuple[ResolvedSegment, ...], terms: tuple[CorrectionTerm, ...]
) -> tuple[tuple[ResolvedSegment, ...], int]:
    """辞書置換を適用し、(新セグメント, 置換ヒット数) を返す（純粋・text のみ変更）。"""
    dictionary = _build_dictionary(terms)
    if dictionary.pattern is None:
        return segments, 0

    hits = 0
    new_segments: list[ResolvedSegment] = []
    for seg in segments:
        text, replaced = dictionary.substitute(seg.text)
        hits += replaced
        new_segments.append(_replace_text(seg, text) if replaced else seg)
    return tuple(new_segments), hits


# ---------------------------------------------------------------------------
# LLM 段（Bedrock・1 回・差分のみ・BR-CORR-01/02）
# ---------------------------------------------------------------------------
def build_prompt(segments: tuple[ResolvedSegment, ...], terms: tuple[CorrectionTerm, ...]) -> str:
    """番号付きセグメント＋用語リストからプロンプトを組み立てる（純粋）。"""
    term_lines = [f"- {t.canonical}" + (f"（誤認識例: {', '.join(t.aliases)}）" if t.aliases else "") for t in terms]
    seg_lines = [f"{i}\t{seg.text}" for i, seg in enumerate(segments)]
    return f"{_INSTRUCTION}\n\n# 用語リスト\n" + "\n".join(term_lines) + "\n\n# 行\n" + "\n".join(seg_lines)


def apply_llm_corrections(
    segments: tuple[ResolvedSegment, ...], payload: bytes | str
) -> tuple[tuple[ResolvedSegment, ...], int]:
    """LLM 応答（差分 JSON）をセグメントへ適用し、(新セグメント, 変更数) を返す。

    範囲外 id・重複・JSON 崩れは握り潰さず不採用に倒す（ValueError 送出 → 呼び出し側でフォールバック）。
    """
    corrections = _parse_corrections(payload)
    n = len(segments)
    texts = [seg.text for seg in segments]
    changed = 0
    applied_ids: set[int] = set()
    for item in corrections:
        idx = item["id"]
        if not isinstance(idx, int) or idx < 0 or idx >= n:
            raise ValueError(f"LLM 応答に範囲外の id があります: {idx}")
        if idx in applied_ids:
            raise ValueError(f"LLM 応答に重複 id があります: {idx}")
        applied_ids.add(idx)
        new_text = str(item["text"])
        if new_text != texts[idx]:
            texts[idx] = new_text
            changed += 1

    new_segments = tuple(
        seg if texts[i] == seg.text else _replace_text(seg, texts[i]) for i, seg in enumerate(segments)
    )
    return new_segments, changed


def _parse_corrections(payload: bytes | str) -> list[dict[str, Any]]:
    """Bedrock(Claude messages) 応答本文 → corrections 配列（純粋・崩れは ValueError）。"""
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("LLM 応答の解析に失敗しました。") from exc
    content = data.get("content", [])
    text = "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
    if not text:
        raise ValueError("LLM 応答にテキストが含まれていません。")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("LLM 応答本文が JSON ではありません。") from exc
    corrections = parsed.get("corrections")
    if not isinstance(corrections, list):
        raise ValueError("LLM 応答に corrections 配列がありません。")
    for item in corrections:
        if not isinstance(item, dict) or "id" not in item or "text" not in item:
            raise ValueError("corrections の要素が不正です。")
    return corrections


# ---------------------------------------------------------------------------
# オーケストレーション（FR-C2-07）
# ---------------------------------------------------------------------------
def correct(
    named: FinalTranscript,
    config: PipelineConfig,
    terms: tuple[CorrectionTerm, ...],
    client: Any | None = None,
) -> CorrectionOutcome:
    """命名済 FinalTranscript を補正する。辞書→LLM の順。client は注入可能（テスト）。"""
    if not terms:
        return CorrectionOutcome(transcript=named, status=CorrectionStatus.SKIPPED)

    seg_dict, hits = apply_dictionary(named.segments, terms)

    model = config.require_bedrock_model()
    try:
        runtime = client if client is not None else build_client("bedrock-runtime", config.aws_region, stage=_STAGE)
        payload = _invoke(runtime, model, build_prompt(seg_dict, terms))
        seg_llm, changed = apply_llm_corrections(seg_dict, payload)
        status = CorrectionStatus.APPLIED
    except Exception as exc:  # noqa: BLE001 フェイルセーフ（NFR-C2-03）
        # LLM 段の失敗は議事録生成を止めない。辞書分は維持し警告で顕在化。
        logger.warning("LLM 補正に失敗しました（辞書分は適用済み・処理継続）: %s", exc)
        seg_llm, changed = seg_dict, 0
        status = CorrectionStatus.DICTIONARY_ONLY if hits else CorrectionStatus.FALLBACK

    outcome = CorrectionOutcome(
        transcript=named,  # メタ付与時に segments ごと差し替える
        status=status,
        dictionary_hits=hits,
        llm_changed=changed,
        model=model,
    )
    return _finalize(outcome, named, seg_llm)


def _invoke(runtime: Any, model_id: str, prompt: str) -> bytes:
    body = json.dumps(
        {
            "anthropic_version": _ANTHROPIC_VERSION,
            "max_tokens": _MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        }
    )
    response = runtime.invoke_model(modelId=model_id, body=body)
    payload: bytes = response["body"].read()
    return payload


# ---------------------------------------------------------------------------
# 内部ヘルパ（不変更新）
# ---------------------------------------------------------------------------
def _replace_text(seg: ResolvedSegment, text: str) -> ResolvedSegment:
    """本文だけを差し替えた新しいセグメントを返す（構造不変・BR-CORR-01）。

    `dataclasses.replace` を使う。全フィールドを手で書き写すと、`ResolvedSegment` に項目が
    増えたときここが黙って既定値へ戻す（構造不変の要件を型検査なしで破る）。
    """
    return replace(seg, text=text)


def _finalize(
    outcome: CorrectionOutcome,
    base: FinalTranscript,
    segments: tuple[ResolvedSegment, ...],
) -> CorrectionOutcome:
    """補正後セグメントと補正メタ（modelInfo.correction）を載せた最終 outcome を返す。"""
    info = dict(base.model_info)
    info["correction"] = outcome.meta()
    transcript = FinalTranscript(
        session_id=base.session_id,
        language=base.language,
        segments=segments,
        speakers=base.speakers,
        common_start_utc=base.common_start_utc,
        model_info=info,
        is_partial=base.is_partial,
    )
    return CorrectionOutcome(
        transcript=transcript,
        status=outcome.status,
        dictionary_hits=outcome.dictionary_hits,
        llm_changed=outcome.llm_changed,
        model=outcome.model,
    )
