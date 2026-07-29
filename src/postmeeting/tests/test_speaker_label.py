"""実名割当のテスト（BR-NAME）。クラスタ生成・適用・unresolved・テンプレ生成を検証する。"""

from __future__ import annotations

import json
from pathlib import Path

from subtext_postmeeting.models import FinalTranscript, ResolvedSegment, SpeakerNameMap, StreamRole
from subtext_postmeeting.speaker_label import (
    apply_mapping,
    build_clusters,
    build_template,
    ensure_naming,
)


def _final(segs: list[tuple[str, StreamRole, str]]) -> FinalTranscript:
    resolved = tuple(
        ResolvedSegment(speaker=spk, origin=origin, start_sec=float(i), end_sec=float(i) + 1.0, text=txt)
        for i, (spk, origin, txt) in enumerate(segs)
    )
    speakers = tuple(dict.fromkeys(s.speaker for s in resolved))
    return FinalTranscript(session_id="sess1", language="ja-JP", segments=resolved, speakers=speakers)


class TestBuildClusters:
    def test_clusters_only_others(self) -> None:
        final = _final(
            [("spk_0", StreamRole.OTHERS, "あ"), ("self", StreamRole.SELF, "い"), ("spk_0", StreamRole.OTHERS, "う")]
        )
        clusters = build_clusters(final)
        assert [c.label for c in clusters] == ["spk_0"]
        assert clusters[0].segment_count == 2

    def test_sample_utterances_capped(self) -> None:
        segs = [("spk_0", StreamRole.OTHERS, f"発言{i}") for i in range(5)]
        clusters = build_clusters(_final(segs))
        assert len(clusters[0].sample_utterances) == 3


class TestApplyMapping:
    def test_renames_mapped_speakers(self) -> None:
        final = _final([("spk_0", StreamRole.OTHERS, "あ"), ("self", StreamRole.SELF, "い")])
        name_map = SpeakerNameMap(session_id="sess1", mappings={"spk_0": "田中", "self": "自分"})
        result = apply_mapping(final, name_map)
        assert result.segments[0].speaker == "田中"
        assert result.segments[1].speaker == "自分"

    def test_unmapped_label_kept_and_listed_unresolved(self) -> None:
        final = _final([("spk_0", StreamRole.OTHERS, "あ"), ("spk_1", StreamRole.OTHERS, "い")])
        name_map = SpeakerNameMap(session_id="sess1", mappings={"spk_0": "田中"})
        result = apply_mapping(final, name_map)
        assert result.segments[1].speaker == "spk_1"
        # BR-NAME-02: 未記入ラベルは model_info["naming"]["unresolved"] に列挙される。
        assert result.model_info["naming"]["unresolved"] == ["spk_1"]

    def test_all_resolved_yields_empty_unresolved(self) -> None:
        final = _final([("spk_0", StreamRole.OTHERS, "あ"), ("self", StreamRole.SELF, "い")])
        name_map = SpeakerNameMap(session_id="sess1", mappings={"spk_0": "田中", "self": "自分"})
        result = apply_mapping(final, name_map)
        assert result.model_info["naming"]["unresolved"] == []

    def test_empty_name_treated_as_unmapped(self) -> None:
        final = _final([("spk_0", StreamRole.OTHERS, "あ")])
        name_map = SpeakerNameMap(session_id="sess1", mappings={"spk_0": "  "})
        result = apply_mapping(final, name_map)
        assert result.segments[0].speaker == "spk_0"

    def test_apply_is_non_destructive(self) -> None:
        final = _final([("spk_0", StreamRole.OTHERS, "あ")])
        apply_mapping(final, SpeakerNameMap(session_id="sess1", mappings={"spk_0": "田中"}))
        assert final.segments[0].speaker == "spk_0"  # 元は不変


class TestTemplate:
    def test_template_includes_self_default_and_blank_others(self) -> None:
        final = _final([("spk_0", StreamRole.OTHERS, "あ")])
        template = build_template(final)
        assert template["mappings"]["self"] == "自分"
        assert template["mappings"]["spk_0"] == ""
        assert "_clusters" in template


class TestEnsureNaming:
    def test_writes_template_and_returns_none_when_missing(self, tmp_path: Path) -> None:
        final = _final([("spk_0", StreamRole.OTHERS, "あ")])
        naming = tmp_path / "speaker_names.json"
        result = ensure_naming(final, naming)
        assert result is None
        assert naming.is_file()

    def test_reads_existing_mapping(self, tmp_path: Path) -> None:
        naming = tmp_path / "speaker_names.json"
        naming.write_text(
            json.dumps({"sessionId": "sess1", "mappings": {"spk_0": "田中"}}, ensure_ascii=False),
            encoding="utf-8",
        )
        final = _final([("spk_0", StreamRole.OTHERS, "あ")])
        result = ensure_naming(final, naming)
        assert result is not None
        assert result.mappings["spk_0"] == "田中"
