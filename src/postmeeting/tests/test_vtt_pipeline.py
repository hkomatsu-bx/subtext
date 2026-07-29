"""VTT 経路の統合テスト（命名ゲート初期値・--no-summarize）。AWS は使わない。

run_from_transcript は Transcribe/S3 を踏まないため、summarize=False（Bedrock 未課金）の
範囲ならローカルのみで完結する。命名ゲートに VTT 話者名が初期値として入ることも確認する。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from subtext_postmeeting.config import PipelineConfig
from subtext_postmeeting.errors import PipelineError
from subtext_postmeeting.pipeline import PostMeetingPipeline, ResultStatus
from subtext_postmeeting.speaker_label import build_template
from subtext_postmeeting.vtt_parser import parse_vtt

_VTT = (
    "WEBVTT\n\n"
    "00:00:01.000 --> 00:00:03.000\n<v 田中>おはよう</v>\n\n"
    "00:00:04.000 --> 00:00:06.000\n<v 佐藤>こんにちは</v>\n"
)


def _config(tmp_path: Path) -> PipelineConfig:
    return PipelineConfig(
        aws_region="ap-northeast-1",
        s3_bucket="",
        s3_prefix="",
        language="ja-JP",
        max_speakers=5,
        poll_timeout_sec=1800,
        keep_s3=False,
        output_dir=tmp_path,
        bedrock_model_id="",
        vocabulary_name="",
        correction_terms_path=tmp_path / "no-terms.json",  # 非存在→補正 no-op
    )


def _write_newer(path: Path, text: str, *, than: Path) -> None:
    """`than` より確実に新しい mtime で書く（同一 tick で更新検知を取りこぼさないため）。"""
    path.write_text(text, encoding="utf-8")
    newer = than.stat().st_mtime + 2
    os.utime(path, (newer, newer))


def test_template_prefills_vtt_names() -> None:
    ft = parse_vtt(_VTT, "m", "ja-JP")
    tpl = build_template(ft)
    assert tpl["mappings"]["spk_0"] == "田中"
    assert tpl["mappings"]["spk_1"] == "佐藤"


def test_template_marks_vtt_names_as_unconfirmed_hints() -> None:
    """VTT 由来の話者名は「候補」として記録し、人が確認した実名と区別すること。

    ヒントは実名とは限らない（ライブ字幕は「自分」「相手」、Teams は「Speaker 1」を出す）。
    値が入っていることを確認済みと解釈すると下流の命名ゲートが素通りする（BR-NAME-01）。
    """
    tpl = build_template(parse_vtt(_VTT, "m", "ja-JP"))
    assert tpl["_hintedLabels"] == ["spk_0", "spk_1"]


def test_template_without_hints_has_no_hinted_labels() -> None:
    """Transcribe 経路（ヒント無し）は従来どおり空欄＝記入待ちで、余計なキーも増やさない。"""
    machine_vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v spk_0>おはよう</v>\n"
    tpl = build_template(parse_vtt(machine_vtt, "m", "ja-JP"))
    assert tpl["mappings"]["spk_0"] == ""
    assert "_hintedLabels" not in tpl


def test_vtt_path_stops_for_naming_then_completes_without_summarize(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    pipe = PostMeetingPipeline(cfg)
    merged = parse_vtt(_VTT, "sess1", "ja-JP")

    # 1回目: 命名ゲートで停止し、テンプレに VTT 名が初期値で入る。
    r1 = pipe.run_from_transcript(merged, summarize=False)
    assert r1.status == ResultStatus.NAMING_REQUIRED
    naming = tmp_path / "sess1" / "speaker_names.json"
    data = json.loads(naming.read_text(encoding="utf-8"))
    assert data["mappings"]["spk_0"] == "田中"
    assert data["mappings"]["spk_1"] == "佐藤"

    # 名前を確認（必要なら補正）して再実行 → final まで生成（Bedrock 未使用）。
    r2 = pipe.run_from_transcript(merged, summarize=False)
    assert r2.status == ResultStatus.SUMMARIZE_SKIPPED
    final = json.loads((tmp_path / "sess1" / "final_transcript.json").read_text(encoding="utf-8"))
    speakers = {s["speaker"] for s in final["segments"]}
    assert speakers == {"田中", "佐藤"}


def test_vtt_path_refuses_to_reuse_another_meetings_session(tmp_path: Path) -> None:
    """同名の別ファイル（Teams 既定の Recording.vtt 等）で前の会議の成果物を再利用しないこと。

    セッションIDは入力ファイル名（拡張子なし）由来のため、別会議でも同名なら同じ出力
    ディレクトリを共有する。段の再利用がそれを「続きから」と解釈すると、前の会議の実名入り
    議事録を成功として返し、今回の内容はどこにも書かれない（静かな取り違え＋別会議の PII 共有）。
    ライブ字幕経路は毎回 `live_captions.vtt` になるため、この衝突は確実に起きる。
    """
    cfg = _config(tmp_path)
    pipe = PostMeetingPipeline(cfg)
    pipe.run_from_transcript(parse_vtt(_VTT, "Recording", "ja-JP"), summarize=False)  # 命名ゲートで停止

    other = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n<v 鈴木>別の会議の話です</v>\n"

    with pytest.raises(PipelineError) as ei:
        pipe.run_from_transcript(parse_vtt(other, "Recording", "ja-JP"), summarize=False)

    assert "別の入力" in str(ei.value)
    # 前の会議の成果物は書き換えない（退避の判断は人に委ねる）。
    merged_json = (tmp_path / "Recording" / "final_transcript.merged.json").read_text(encoding="utf-8")
    assert "おはよう" in merged_json
    assert "別の会議の話です" not in merged_json


def test_vtt_path_reruns_naming_when_speaker_names_updated(tmp_path: Path) -> None:
    """speaker_names.json を修正して再実行したら反映されること（黙って再利用しない, BR-NAME-01）。

    命名ゲートの運用そのものが「記入・修正して再実行」なのに、成果物の存在だけで再利用すると
    修正が無視され、古い名前のまま rc=0・「完了」と報告される（修正手段が他に無い）。
    """
    cfg = _config(tmp_path)
    pipe = PostMeetingPipeline(cfg)
    merged = parse_vtt(_VTT, "sess-fix", "ja-JP")

    pipe.run_from_transcript(merged, summarize=False)  # 命名ゲートで停止
    pipe.run_from_transcript(merged, summarize=False)  # テンプレ初期値（田中/佐藤）で確定

    naming = tmp_path / "sess-fix" / "speaker_names.json"
    named = tmp_path / "sess-fix" / "final_transcript.named.json"
    data = json.loads(naming.read_text(encoding="utf-8"))
    data["mappings"]["spk_0"] = "田中太郎"  # 姓だけだった初期値を人手で補正
    _write_newer(naming, json.dumps(data, ensure_ascii=False, indent=2), than=named)

    pipe.run_from_transcript(merged, summarize=False)

    final = json.loads((tmp_path / "sess-fix" / "final_transcript.json").read_text(encoding="utf-8"))
    assert {s["speaker"] for s in final["segments"]} == {"田中太郎", "佐藤"}
