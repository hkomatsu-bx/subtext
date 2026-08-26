"""C2 LLM 後処理補正のテスト（純粋部＋パイプライン結合）。AWS は使わない（注入）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from subtext_postmeeting.config import PipelineConfig
from subtext_postmeeting.correction import (
    CorrectionOutcome,
    CorrectionStatus,
    CorrectionTerm,
    apply_dictionary,
    apply_llm_corrections,
    build_prompt,
    correct,
    load_terms,
)
from subtext_postmeeting.errors import PipelineError
from subtext_postmeeting.models import FinalTranscript, ResolvedSegment, StreamRole
from subtext_postmeeting.pipeline import PostMeetingPipeline, ResultStatus

_TERMS = (
    CorrectionTerm(canonical="BeeX", aliases=("bx", "ビーエックス")),
    CorrectionTerm(canonical="Bedrock", aliases=("ベトロック",)),
)


def _seg(text: str, speaker: str = "spk_0") -> ResolvedSegment:
    return ResolvedSegment(speaker=speaker, origin=StreamRole.OTHERS, start_sec=0.0, end_sec=1.0, text=text)


def _bedrock_payload(corrections: list[dict]) -> bytes:
    """Bedrock(Claude messages) 応答を模した bytes を返す。"""
    inner = json.dumps({"corrections": corrections}, ensure_ascii=False)
    return json.dumps({"content": [{"type": "text", "text": inner}]}).encode("utf-8")


# ---------------------------------------------------------------------------
# load_terms
# ---------------------------------------------------------------------------
class TestLoadTerms:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_terms(tmp_path / "none.json") == ()

    def test_empty_terms_returns_empty(self, tmp_path: Path) -> None:
        p = tmp_path / "t.json"
        p.write_text(json.dumps({"terms": []}), encoding="utf-8")
        assert load_terms(p) == ()

    def test_valid_terms(self, tmp_path: Path) -> None:
        p = tmp_path / "t.json"
        p.write_text(
            json.dumps({"terms": [{"canonical": "BeeX", "aliases": ["bx", "ビーエックス"]}]}),
            encoding="utf-8",
        )
        terms = load_terms(p)
        assert terms[0].canonical == "BeeX"
        assert terms[0].aliases == ("bx", "ビーエックス")

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "t.json"
        p.write_text("{not json", encoding="utf-8")
        with pytest.raises(PipelineError):
            load_terms(p)

    def test_empty_canonical_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "t.json"
        p.write_text(json.dumps({"terms": [{"canonical": "", "aliases": ["x"]}]}), encoding="utf-8")
        with pytest.raises(PipelineError):
            load_terms(p)

    def test_aliases_as_string_raises_with_actionable_message(self, tmp_path: Path) -> None:
        """`"aliases": "bx"` は配列でないと 1 文字ずつに展開されるため、読込時に止めること。

        文字列を反復すると `b`, `x` という 1 文字エイリアスになり、ASCII は語境界付き・大小無視で
        一致するため「b」「x」という単独の語を無関係な箇所で書き換えてしまう（設定ミスが黙って
        本文を壊す）。書き方を示して停止する（NFR-C2-03: 設定ミスの早期顕在化）。
        """
        p = tmp_path / "t.json"
        p.write_text(json.dumps({"terms": [{"canonical": "BeeX", "aliases": "bx"}]}), encoding="utf-8")

        with pytest.raises(PipelineError, match="配列で指定"):
            load_terms(p)

    def test_short_japanese_alias_raises(self, tmp_path: Path) -> None:
        """短い和文エイリアスは受け付けないこと（無関係な語の内側に当たって本文を壊す）。

        和文は語境界を判定できず素朴な部分一致になる。同梱の terms.json にあった 2 文字の
        「イジ」は「エンゲイジメント」「ペイジ」「デイジー」に一致し、辞書段（決定的・非課金で
        既定経路では必ず通る）が final_transcript.json → 議事録 → Slack まで壊れた本文を流していた。
        """
        p = tmp_path / "t.json"
        p.write_text(json.dumps({"terms": [{"canonical": "1G", "aliases": ["イジ"]}]}), encoding="utf-8")

        with pytest.raises(PipelineError, match="短すぎます"):
            load_terms(p)

    def test_short_ascii_alias_is_allowed(self, tmp_path: Path) -> None:
        """ASCII は語境界付きで一致するため短くても安全（bx 等の既存運用を壊さない）。"""
        p = tmp_path / "t.json"
        p.write_text(json.dumps({"terms": [{"canonical": "BeeX", "aliases": ["bx"]}]}), encoding="utf-8")

        assert load_terms(p)[0].aliases == ("bx",)

    def test_shipped_terms_file_is_valid(self) -> None:
        """リポジトリ同梱の terms.json が現在の検証を通ること（危険なエイリアスの再混入を防ぐ）。"""
        shipped = Path(__file__).resolve().parents[3] / "tools" / "correction" / "terms.json"

        terms = load_terms(shipped)

        assert terms, "同梱の用語リストが読めること"
        for term in terms:
            for alias in term.aliases:
                assert alias.isascii() or len(alias) >= 3, f"{term.canonical}: {alias}"


# ---------------------------------------------------------------------------
# apply_dictionary（決定的・語境界）
# ---------------------------------------------------------------------------
class TestApplyDictionary:
    def test_ascii_word_boundary(self) -> None:
        segs, hits = apply_dictionary((_seg("bx の件"),), _TERMS)
        assert segs[0].text == "BeeX の件"
        assert hits == 1

    def test_ascii_no_partial_match(self) -> None:
        # 'bx' が他語の部分文字列（abx, bxy）のときは置換しない。
        segs, hits = apply_dictionary((_seg("abx bxy"),), _TERMS)
        assert segs[0].text == "abx bxy"
        assert hits == 0

    def test_ascii_case_insensitive(self) -> None:
        segs, _ = apply_dictionary((_seg("BX を使う"),), _TERMS)
        assert segs[0].text == "BeeX を使う"

    def test_japanese_alias(self) -> None:
        segs, hits = apply_dictionary((_seg("ビーエックスとベトロック"),), _TERMS)
        assert segs[0].text == "BeeXとBedrock"
        assert hits == 2

    def test_canonical_with_backslash_is_literal(self) -> None:
        """正規表記のバックスラッシュを置換テンプレートとして解釈しないこと。

        `re.sub` の置換文字列はテンプレートで、`\\1` は後方参照、末尾の単独 `\\` は
        `re.PatternError` になる。用語ファイルは人が書くため（Windows パスや `C:\\` を含む
        表記もあり得る）、ここで落ちると **Bedrock 課金後に生トレースバック**で止まる。
        """
        terms = (CorrectionTerm(canonical=r"C:\work\1", aliases=("cwork",)),)

        segs, hits = apply_dictionary((_seg("cwork を見て"),), terms)

        assert segs[0].text == r"C:\work\1 を見て"
        assert hits == 1

    def test_canonical_with_trailing_backslash_does_not_raise(self) -> None:
        terms = (CorrectionTerm(canonical="path\\", aliases=("pth",)),)

        segs, _ = apply_dictionary((_seg("pth"),), terms)

        assert segs[0].text == "path\\"

    def test_structure_invariant(self) -> None:
        seg = ResolvedSegment(
            speaker="Alice",
            origin=StreamRole.SELF,
            start_sec=1.5,
            end_sec=2.5,
            text="bx",
            confidence=0.9,
        )
        segs, _ = apply_dictionary((seg,), _TERMS)
        out = segs[0]
        assert (out.speaker, out.origin, out.start_sec, out.end_sec, out.confidence) == (
            "Alice",
            StreamRole.SELF,
            1.5,
            2.5,
            0.9,
        )

    def test_no_terms_noop(self) -> None:
        segs, hits = apply_dictionary((_seg("bx"),), ())
        assert segs[0].text == "bx" and hits == 0


# ---------------------------------------------------------------------------
# build_prompt / apply_llm_corrections
# ---------------------------------------------------------------------------
class TestLlm:
    def test_build_prompt_contains_ids_and_terms(self) -> None:
        prompt = build_prompt((_seg("a"), _seg("b")), _TERMS)
        assert "0\ta" in prompt and "1\tb" in prompt
        assert "BeeX" in prompt

    def test_apply_diff(self) -> None:
        segs = (_seg("bx の件"), _seg("変更なし"))
        out, changed = apply_llm_corrections(segs, _bedrock_payload([{"id": 0, "text": "BeeX の件"}]))
        assert out[0].text == "BeeX の件"
        assert out[1].text == "変更なし"
        assert changed == 1

    def test_empty_corrections_noop(self) -> None:
        segs = (_seg("a"),)
        out, changed = apply_llm_corrections(segs, _bedrock_payload([]))
        assert out[0].text == "a" and changed == 0

    def test_out_of_range_id_raises(self) -> None:
        with pytest.raises(ValueError):
            apply_llm_corrections((_seg("a"),), _bedrock_payload([{"id": 5, "text": "x"}]))

    def test_duplicate_id_raises(self) -> None:
        with pytest.raises(ValueError):
            apply_llm_corrections(
                (_seg("a"), _seg("b")),
                _bedrock_payload([{"id": 0, "text": "x"}, {"id": 0, "text": "y"}]),
            )

    def test_garbage_response_raises(self) -> None:
        bad = json.dumps({"content": [{"type": "text", "text": "これはJSONではない"}]}).encode()
        with pytest.raises(ValueError):
            apply_llm_corrections((_seg("a"),), bad)


# ---------------------------------------------------------------------------
# correct（オーケストレーション）
# ---------------------------------------------------------------------------
def _cfg(tmp_path: Path, model: str = "fake-model") -> PipelineConfig:
    return PipelineConfig(
        aws_region="ap-northeast-1",
        s3_bucket="",
        s3_prefix="",
        language="ja-JP",
        max_speakers=5,
        poll_timeout_sec=10,
        keep_s3=False,
        output_dir=tmp_path,
        bedrock_model_id=model,
        vocabulary_name="",
        correction_terms_path=tmp_path / "n.json",
    )


def _transcript() -> FinalTranscript:
    return FinalTranscript(session_id="s", language="ja-JP", segments=(_seg("bx と ベトロック"),), speakers=("spk_0",))


class _FakeClient:
    def __init__(self, payload: bytes | None = None, raise_exc: bool = False) -> None:
        self._payload = payload
        self._raise = raise_exc

    def invoke_model(self, modelId: str, body: str):  # noqa: N803 (boto3 互換)
        if self._raise:
            raise RuntimeError("boom")
        return {"body": _Body(self._payload or b"")}


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class TestCorrect:
    def test_terms_empty_skipped(self, tmp_path: Path) -> None:
        out = correct(_transcript(), _cfg(tmp_path), ())
        assert out.status == CorrectionStatus.SKIPPED
        assert out.transcript.segments[0].text == "bx と ベトロック"

    def test_applied_dictionary_plus_llm(self, tmp_path: Path) -> None:
        # 辞書で bx→BeeX・ベトロック→Bedrock。LLM はさらに差分を返さず空。
        client = _FakeClient(_bedrock_payload([]))
        out = correct(_transcript(), _cfg(tmp_path), _TERMS, client=client)
        assert out.status == CorrectionStatus.APPLIED
        assert out.transcript.segments[0].text == "BeeX と Bedrock"
        assert out.dictionary_hits == 2
        assert out.transcript.model_info["correction"]["status"] == "applied"

    def test_llm_failure_falls_back_to_dictionary(self, tmp_path: Path) -> None:
        client = _FakeClient(raise_exc=True)
        out = correct(_transcript(), _cfg(tmp_path), _TERMS, client=client)
        assert out.status == CorrectionStatus.DICTIONARY_ONLY  # 辞書ヒットあり
        assert out.transcript.segments[0].text == "BeeX と Bedrock"  # 辞書分は維持

    def test_llm_failure_no_dict_hit_is_fallback(self, tmp_path: Path) -> None:
        ft = FinalTranscript(session_id="s", language="ja-JP", segments=(_seg("辞書外の語"),), speakers=("spk_0",))
        client = _FakeClient(raise_exc=True)
        out = correct(ft, _cfg(tmp_path), _TERMS, client=client)
        assert out.status == CorrectionStatus.FALLBACK


# ---------------------------------------------------------------------------
# パイプライン結合（run_from_transcript・命名は事前充填）
# ---------------------------------------------------------------------------
def _pipeline_config(tmp_path: Path, *, with_terms: bool) -> PipelineConfig:
    terms_path = tmp_path / "terms.json"
    if with_terms:
        terms_path.write_text(json.dumps({"terms": [{"canonical": "BeeX", "aliases": ["bx"]}]}), encoding="utf-8")
    return PipelineConfig(
        aws_region="ap-northeast-1",
        s3_bucket="",
        s3_prefix="",
        language="ja-JP",
        max_speakers=5,
        poll_timeout_sec=10,
        keep_s3=False,
        output_dir=tmp_path,
        bedrock_model_id="fake-model",
        vocabulary_name="",
        correction_terms_path=terms_path,
    )


def _prefill_naming(tmp_path: Path, session: str) -> None:
    d = tmp_path / session
    d.mkdir(parents=True, exist_ok=True)
    (d / "speaker_names.json").write_text(
        json.dumps({"sessionId": session, "mappings": {"spk_0": "Alice"}, "unresolved": []}),
        encoding="utf-8",
    )


def _merged(session: str = "s") -> FinalTranscript:
    return FinalTranscript(session_id=session, language="ja-JP", segments=(_seg("bx の話"),), speakers=("spk_0",))


def _fake_corrector_factory(counter: list[int]):
    def _fake(named: FinalTranscript, config: PipelineConfig, terms) -> CorrectionOutcome:
        counter.append(1)
        seg = named.segments[0]
        fixed = ResolvedSegment(
            speaker=seg.speaker,
            origin=seg.origin,
            start_sec=seg.start_sec,
            end_sec=seg.end_sec,
            text=seg.text.replace("bx", "BeeX"),
            confidence=seg.confidence,
        )
        info = dict(named.model_info)
        info["correction"] = {
            "applied": True,
            "status": "applied",
            "model": "fake",
            "dictionaryHits": 0,
            "llmChanged": 1,
        }
        return CorrectionOutcome(
            transcript=FinalTranscript(
                session_id=named.session_id,
                language=named.language,
                segments=(fixed,),
                speakers=named.speakers,
                common_start_utc=named.common_start_utc,
                model_info=info,
                is_partial=named.is_partial,
            ),
            status=CorrectionStatus.APPLIED,
            dictionary_hits=0,
            llm_changed=1,
            model="fake",
        )

    return _fake


class TestPipelineWiring:
    def test_correction_applied_and_named_preserved(self, tmp_path: Path) -> None:
        _prefill_naming(tmp_path, "s")
        creds: list[int] = []
        calls: list[int] = []
        pipe = PostMeetingPipeline(
            _pipeline_config(tmp_path, with_terms=True),
            corrector=_fake_corrector_factory(calls),
            credential_checker=lambda: creds.append(1),
        )
        r = pipe.run_from_transcript(_merged(), summarize=False)
        assert r.status == ResultStatus.SUMMARIZE_SKIPPED
        # final は補正後（BeeX）。named（補正前 bx）は別ファイルに保持。
        final = json.loads((tmp_path / "s" / "final_transcript.json").read_text("utf-8"))
        named = json.loads((tmp_path / "s" / "final_transcript.named.json").read_text("utf-8"))
        assert final["segments"][0]["text"] == "BeeX の話"
        assert named["segments"][0]["text"] == "bx の話"
        assert final["modelInfo"]["correction"]["status"] == "applied"
        assert calls == [1]  # 補正1回
        assert creds == [1]  # LLM 前に資格情報確認

    def test_no_correct_skips_correction(self, tmp_path: Path) -> None:
        _prefill_naming(tmp_path, "s")
        creds: list[int] = []
        calls: list[int] = []
        pipe = PostMeetingPipeline(
            _pipeline_config(tmp_path, with_terms=True),
            corrector=_fake_corrector_factory(calls),
            credential_checker=lambda: creds.append(1),
        )
        r = pipe.run_from_transcript(_merged(), summarize=False, correct=False)
        assert r.status == ResultStatus.SUMMARIZE_SKIPPED
        final = json.loads((tmp_path / "s" / "final_transcript.json").read_text("utf-8"))
        assert final["segments"][0]["text"] == "bx の話"  # 無補正
        assert calls == [] and creds == []  # 補正も資格情報確認も走らない

    def test_empty_terms_noop(self, tmp_path: Path) -> None:
        _prefill_naming(tmp_path, "s")
        calls: list[int] = []
        pipe = PostMeetingPipeline(
            _pipeline_config(tmp_path, with_terms=False),  # 用語ファイル無し
            corrector=_fake_corrector_factory(calls),
            credential_checker=lambda: None,
        )
        r = pipe.run_from_transcript(_merged(), summarize=False)
        assert r.status == ResultStatus.SUMMARIZE_SKIPPED
        final = json.loads((tmp_path / "s" / "final_transcript.json").read_text("utf-8"))
        assert final["segments"][0]["text"] == "bx の話"
        assert calls == []  # 用語なし→補正器は呼ばれない

    def test_reuse_then_force(self, tmp_path: Path) -> None:
        _prefill_naming(tmp_path, "s")
        calls: list[int] = []
        cfg = _pipeline_config(tmp_path, with_terms=True)
        pipe = PostMeetingPipeline(cfg, corrector=_fake_corrector_factory(calls), credential_checker=lambda: None)
        pipe.run_from_transcript(_merged(), summarize=False)
        pipe.run_from_transcript(_merged(), summarize=False)  # final 再利用
        assert calls == [1]  # 2回目は補正再利用
        pipe.run_from_transcript(_merged(), summarize=False, force=True)  # 再補正
        assert calls == [1, 1]

    def test_summarize_receives_corrected(self, tmp_path: Path) -> None:
        _prefill_naming(tmp_path, "s")
        seen: list[str] = []

        def fake_summarizer(transcript: FinalTranscript, config: PipelineConfig, **_kwargs: object):
            seen.append(transcript.segments[0].text)
            from subtext_postmeeting.models import MinutesDoc
            from datetime import datetime, timezone

            return MinutesDoc(
                session_id=transcript.session_id,
                markdown="# m",
                source_model="x",
                generated_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )

        pipe = PostMeetingPipeline(
            _pipeline_config(tmp_path, with_terms=True),
            summarizer=fake_summarizer,
            corrector=_fake_corrector_factory([]),
            credential_checker=lambda: None,
        )
        r = pipe.run_from_transcript(_merged(), summarize=True)
        assert r.status == ResultStatus.COMPLETED
        assert seen == ["BeeX の話"]  # 要約段は補正後を受け取る


class TestApplyDictionarySinglePass:
    """辞書段は 1 パス（S-1）。置換で生まれた正規表記を再走査しない。"""

    def test_replacement_result_is_not_rescanned(self) -> None:
        """`bx`→`BeeX` の結果に、別エイリアス `Bee` が連鎖して当たらないこと。

        エイリアスごとに subn を回す実装では、先に `bx → BeeX` が起き、その `Bee` に
        後続ルールが当たって `<社名>X` のような二重置換になる（本文が壊れる）。
        """
        terms = (
            CorrectionTerm(canonical="BeeX", aliases=("bx",)),
            CorrectionTerm(canonical="<社名>", aliases=("Bee",)),
        )

        segs, hits = apply_dictionary((_seg("bx の件"),), terms)

        assert segs[0].text == "BeeX の件"
        assert hits == 1

    def test_longest_alias_wins_at_same_position(self) -> None:
        """同じ位置に複数のエイリアスが当たる場合は長い方を採る（交替は長い順に並べる）。"""
        terms = (
            CorrectionTerm(canonical="長", aliases=("ビーエックス",)),
            CorrectionTerm(canonical="短", aliases=("ビーエ",)),
        )

        segs, hits = apply_dictionary((_seg("ビーエックスの話"),), terms)

        assert segs[0].text == "長の話"
        assert hits == 1

    def test_unchanged_segment_is_returned_as_is(self) -> None:
        """置換が無いセグメントは同一オブジェクトを返す（無駄なコピーをしない）。"""
        seg = _seg("無関係な本文")
        segs, hits = apply_dictionary((seg,), _TERMS)

        assert segs[0] is seg
        assert hits == 0

    def test_ascii_alias_matches_case_insensitively_in_single_pass(self) -> None:
        """1 パス化後も ASCII の大小無視が効くこと（要素ごとのインラインフラグ）。"""
        segs, _ = apply_dictionary((_seg("Bx と bX と BX"),), _TERMS)
        assert segs[0].text == "BeeX と BeeX と BeeX"

    def test_japanese_alias_stays_case_sensitive(self) -> None:
        """和文は厳密一致のまま（パターン全体に IGNORECASE を掛けていないこと）。"""
        terms = (CorrectionTerm(canonical="OK", aliases=("ａｂｃ",)),)

        segs, hits = apply_dictionary((_seg("ＡＢＣ"),), terms)

        assert segs[0].text == "ＡＢＣ"
        assert hits == 0
