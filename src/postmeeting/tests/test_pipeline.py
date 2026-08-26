"""オーケストレータのテスト（BR-PIPE）。AWS 境界はモック化し、段スキップ再利用・naming 停止を検証する。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from subtext_postmeeting.config import PipelineConfig
from subtext_postmeeting.errors import PipelineError
from subtext_postmeeting.input_resolver import resolve_paired, resolve_single
from subtext_postmeeting.models import FinalTranscript, MinutesDoc, Stage, StreamRole
from subtext_postmeeting.pipeline import PipelineOptions, PostMeetingPipeline, ResultStatus

from .conftest import others_transcribe_json, self_transcribe_json


class FakeS3Io:
    """S3Io の最小フェイク。AWS を呼ばず Transcribe 出力 JSON を返す。

    `download_json` は **キーごとに違う payload を返す**（`.../self.json` は self 側）。key を
    無視して同じ JSON を返すと、self/others の取り違え（自分の発言が丸ごと落ちる等）が
    構造的に検出できず、テストが緑のままバグを通す。
    """

    def __init__(self, others_json: dict[str, Any], self_json: dict[str, Any] | None = None) -> None:
        self.bucket = "fake-bucket"
        self._others_json = others_json
        self._self_json = self_json if self_json is not None else self_transcribe_json()
        self.uploaded: list[str] = []
        self.downloaded: list[str] = []
        self.deleted_prefixes: list[str] = []
        self.verified = 0

    def verify_preconditions(self) -> None:
        self.verified += 1

    def upload(self, local_path: Path, key: str) -> str:
        self.uploaded.append(key)
        return f"s3://{self.bucket}/{key}"

    def download_json(self, key: str) -> dict[str, Any]:
        self.downloaded.append(key)
        return self._self_json if key.endswith("self.json") else self._others_json

    def delete_prefix(self, prefix: str) -> int:
        self.deleted_prefixes.append(prefix)
        return 0


class FakeTranscribe:
    def __init__(self) -> None:
        self.calls = 0

    def run_job(self, spec: Any, bucket: str, output_key: str, timeout: int) -> str:
        self.calls += 1
        return output_key


def _fake_summarizer_factory(counter: dict[str, int]):
    def _summarize(transcript: FinalTranscript, config: PipelineConfig, **_kwargs: object) -> MinutesDoc:
        counter["calls"] += 1
        return MinutesDoc(
            session_id=transcript.session_id,
            markdown="## 決定事項\n- なし\n## ToDo\n## 論点・議論サマリ",
            source_model="fake-model",
            generated_at_utc=datetime(2026, 6, 20, tzinfo=timezone.utc),
        )

    return _summarize


def _config(out_dir: Path) -> PipelineConfig:
    return PipelineConfig(
        aws_region="ap-northeast-1",
        s3_bucket="fake-bucket",
        s3_prefix="subtext/jobs/",
        language="ja-JP",
        max_speakers=5,
        poll_timeout_sec=60,
        keep_s3=False,
        output_dir=out_dir,
        bedrock_model_id="fake-model",
        vocabulary_name="",
        correction_terms_path=out_dir / "no-terms.json",  # 非存在→補正 no-op
    )


@pytest.fixture
def single_wav(tmp_path: Path, make_wav) -> Path:
    return make_wav("meeting.wav")


def _build(tmp_path: Path, summ_counter: dict[str, int]):
    s3 = FakeS3Io(others_transcribe_json())
    transcribe = FakeTranscribe()
    pipeline = PostMeetingPipeline(
        _config(tmp_path / "out"),
        s3io=s3,
        transcribe_client=transcribe,
        summarizer=_fake_summarizer_factory(summ_counter),
        credential_checker=lambda: None,  # 事前チェックは no-op（AWS 非依存テスト）
    )
    return pipeline, s3, transcribe


class TestNamingGate:
    def test_first_run_stops_for_naming(self, tmp_path: Path, single_wav: Path) -> None:
        counter = {"calls": 0}
        pipeline, _s3, _tr = _build(tmp_path, counter)
        rec = resolve_single(single_wav, "ja-JP")
        result = pipeline.run(PipelineOptions(input=rec))
        assert result.status == ResultStatus.NAMING_REQUIRED
        naming = tmp_path / "out" / rec.session_id / "speaker_names.json"
        assert naming.is_file()
        assert counter["calls"] == 0  # 議事録は未生成


class TestFullFlow:
    def test_second_run_completes_after_naming(self, tmp_path: Path, single_wav: Path) -> None:
        counter = {"calls": 0}
        pipeline, s3, transcribe = _build(tmp_path, counter)
        rec = resolve_single(single_wav, "ja-JP")

        # 1回目: naming 停止
        pipeline.run(PipelineOptions(input=rec))
        naming = tmp_path / "out" / rec.session_id / "speaker_names.json"
        naming.write_text(
            json.dumps({"sessionId": rec.session_id, "mappings": {"spk_0": "田中", "spk_1": "佐藤"}}),
            encoding="utf-8",
        )

        # 2回目: 完了。Transcribe は再実行されない（中間再利用, BR-PIPE-04）
        result = pipeline.run(PipelineOptions(input=rec))
        assert result.status == ResultStatus.COMPLETED
        assert result.minutes_path.is_file()
        assert result.final_transcript_path.is_file()
        assert transcribe.calls == 1  # 1回目のみ
        assert counter["calls"] == 1

    def test_final_transcript_has_real_names(self, tmp_path: Path, single_wav: Path) -> None:
        counter = {"calls": 0}
        pipeline, _s3, _tr = _build(tmp_path, counter)
        rec = resolve_single(single_wav, "ja-JP")
        pipeline.run(PipelineOptions(input=rec))
        naming = tmp_path / "out" / rec.session_id / "speaker_names.json"
        naming.write_text(
            json.dumps({"sessionId": rec.session_id, "mappings": {"spk_0": "田中", "spk_1": "佐藤"}}),
            encoding="utf-8",
        )
        result = pipeline.run(PipelineOptions(input=rec))
        data = json.loads(result.final_transcript_path.read_text(encoding="utf-8"))
        speakers = set(data["speakers"])
        assert "田中" in speakers and "佐藤" in speakers


class TestNoSummarize:
    def test_no_summarize_skips_bedrock_and_stops_at_final(self, tmp_path: Path, single_wav: Path) -> None:
        counter = {"calls": 0}
        pipeline, _s3, transcribe = _build(tmp_path, counter)
        rec = resolve_single(single_wav, "ja-JP")

        # 1回目: naming 停止（要約段の手前）
        pipeline.run(PipelineOptions(input=rec))
        naming = tmp_path / "out" / rec.session_id / "speaker_names.json"
        naming.write_text(
            json.dumps({"sessionId": rec.session_id, "mappings": {"spk_0": "田中", "spk_1": "佐藤"}}),
            encoding="utf-8",
        )

        # 2回目: --no-summarize 相当。final まで生成し Bedrock は呼ばない
        result = pipeline.run(PipelineOptions(input=rec, summarize=False))
        assert result.status == ResultStatus.SUMMARIZE_SKIPPED
        assert result.minutes_path is None
        assert result.final_transcript_path.is_file()  # 実名適用済 final は生成される
        assert counter["calls"] == 0  # summarize は未実行（Bedrock 未課金）
        minutes = tmp_path / "out" / rec.session_id / "minutes.md"
        assert not minutes.is_file()  # 議事録は外部生成に委ねるため未作成
        assert transcribe.calls == 1  # Transcribe は1回目のみ（再課金なし）

    def test_no_summarize_skips_credential_check_when_raw_present(self, tmp_path: Path, single_wav: Path) -> None:
        # raw が揃った状態での --no-summarize 再実行は実名適用のみ＝AWS 不要。認証チェックを踏まない。
        cred_calls = {"n": 0}

        def _checker() -> None:
            cred_calls["n"] += 1

        counter = {"calls": 0}
        s3 = FakeS3Io(others_transcribe_json())
        transcribe = FakeTranscribe()
        pipeline = PostMeetingPipeline(
            _config(tmp_path / "out"),
            s3io=s3,
            transcribe_client=transcribe,
            summarizer=_fake_summarizer_factory(counter),
            credential_checker=_checker,
        )
        rec = resolve_single(single_wav, "ja-JP")

        pipeline.run(PipelineOptions(input=rec))  # 1回目（Transcribe で認証1回）
        naming = tmp_path / "out" / rec.session_id / "speaker_names.json"
        naming.write_text(
            json.dumps({"sessionId": rec.session_id, "mappings": {"spk_0": "田中", "spk_1": "佐藤"}}),
            encoding="utf-8",
        )
        calls_after_first = cred_calls["n"]

        pipeline.run(PipelineOptions(input=rec, summarize=False))  # raw 再利用・要約なし
        assert cred_calls["n"] == calls_after_first  # 追加の認証チェックは発生しない


class TestStageRerun:
    def test_summarize_only_rerun_does_not_recharge_transcribe(self, tmp_path: Path, single_wav: Path) -> None:
        counter = {"calls": 0}
        pipeline, _s3, transcribe = _build(tmp_path, counter)
        rec = resolve_single(single_wav, "ja-JP")
        pipeline.run(PipelineOptions(input=rec))
        naming = tmp_path / "out" / rec.session_id / "speaker_names.json"
        naming.write_text(
            json.dumps({"sessionId": rec.session_id, "mappings": {"spk_0": "田中", "spk_1": "佐藤"}}),
            encoding="utf-8",
        )
        pipeline.run(PipelineOptions(input=rec))  # complete

        # 議事録のみ再実行
        pipeline.run(PipelineOptions(input=rec, target_stage=Stage.SUMMARIZED))
        assert transcribe.calls == 1  # 再課金なし（BR-PIPE-04）
        assert counter["calls"] == 2  # summarize は再実行


def _write_paired_session(tmp_path: Path, make_wav) -> Path:
    """Unit A の録音セッション（manifest + self/others WAV）を作る。"""
    session_dir = tmp_path / "rec" / "20260620-100000"
    session_dir.mkdir(parents=True)
    for role in ("self", "others"):
        make_wav(f"{role}.wav").replace(session_dir / f"{role}.wav")
    streams = [
        {
            "wavPath": f"{role}.wav",
            "streamRole": role,
            "startTimeUtc": "2026-06-20T01:00:00Z",
            "sampleRate": 16000,
            "channels": 1,
            "bitDepth": 16,
            "deviceName": role,
            "durationSec": 0.1,
            "silenceFilledSec": 0.0,
        }
        for role in ("self", "others")
    ]
    manifest = {
        "sessionId": "20260620-100000",
        "createdAtUtc": "2026-06-20T01:00:05Z",
        "commonStartUtc": "2026-06-20T01:00:00Z",
        "status": "complete",
        "streams": streams,
    }
    (session_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return session_dir


class TestPairedEndToEnd:
    """paired（Unit A の manifest 経由＝本番の主入口）をパイプライン本体で通す。

    これが無いと `rec.mode == InputMode.PAIRED` の分岐がテスト上デッドコードになり、self/others の
    取り違えや self 側の取りこぼし（＝操作者自身の発言が議事録から丸ごと消える）を検出できない。
    """

    def test_transcribes_both_streams_and_keeps_them_apart(self, tmp_path: Path, make_wav) -> None:
        session_dir = _write_paired_session(tmp_path, make_wav)
        counter = {"calls": 0}
        s3 = FakeS3Io(others_transcribe_json(), self_transcribe_json())
        pipeline = PostMeetingPipeline(
            _config(tmp_path / "out"),
            s3io=s3,
            transcribe_client=FakeTranscribe(),
            summarizer=_fake_summarizer_factory(counter),
            credential_checker=lambda: None,
        )
        rec = resolve_paired(session_dir, "ja-JP")

        first = pipeline.run(PipelineOptions(input=rec))
        assert first.status == ResultStatus.NAMING_REQUIRED

        # 2系統ともアップロード・取得され、それぞれ正しい役割のキーに対応すること。
        assert [k.rsplit("/", 1)[-1] for k in s3.uploaded] == ["others.wav", "self.wav"]
        assert [k.rsplit("/", 1)[-1] for k in s3.downloaded] == ["others.json", "self.json"]

        naming = tmp_path / "out" / rec.session_id / "speaker_names.json"
        naming.write_text(
            json.dumps({"sessionId": rec.session_id, "mappings": {"spk_0": "田中", "spk_1": "佐藤"}}),
            encoding="utf-8",
        )
        result = pipeline.run(PipelineOptions(input=rec))

        assert result.status == ResultStatus.COMPLETED
        final = json.loads((tmp_path / "out" / rec.session_id / "final_transcript.json").read_text(encoding="utf-8"))
        origins = {seg["origin"] for seg in final["segments"]}
        assert origins == {StreamRole.SELF.value, StreamRole.OTHERS.value}, "self 系統が落ちていないこと"
        # self 側の本文（conftest の self フィクスチャ由来）が残っていること＝取り違えていない。
        self_texts = " ".join(s["text"] for s in final["segments"] if s["origin"] == StreamRole.SELF.value)
        assert "そうです" in self_texts


class TestMeetingInfoUpdateTriggersResummarize:
    """meeting_info.json の更新は要約段のみを再実行対象にする（BR-MI-01）。"""

    def test_meeting_info_newer_than_minutes_reruns_summarize_only(self, tmp_path: Path, single_wav: Path) -> None:
        counter = {"calls": 0}
        pipeline, _s3, transcribe = _build(tmp_path, counter)
        rec = resolve_single(single_wav, "ja-JP")
        pipeline.run(PipelineOptions(input=rec))  # naming 停止
        naming = tmp_path / "out" / rec.session_id / "speaker_names.json"
        naming.write_text(
            json.dumps({"sessionId": rec.session_id, "mappings": {"spk_0": "田中", "spk_1": "佐藤"}}),
            encoding="utf-8",
        )
        pipeline.run(PipelineOptions(input=rec))  # complete（1回目の summarize）
        assert counter["calls"] == 1

        meeting_info = tmp_path / "out" / rec.session_id / "meeting_info.json"
        meeting_info.write_text(json.dumps({"title": "定例会"}), encoding="utf-8")

        result = pipeline.run(PipelineOptions(input=rec))

        assert result.status == ResultStatus.COMPLETED
        assert counter["calls"] == 2  # summarize のみ再実行
        assert transcribe.calls == 1  # Transcribe は再課金されない

    def test_meeting_info_older_than_minutes_does_not_rerun(self, tmp_path: Path, single_wav: Path) -> None:
        counter = {"calls": 0}
        pipeline, _s3, _tr = _build(tmp_path, counter)
        rec = resolve_single(single_wav, "ja-JP")
        pipeline.run(PipelineOptions(input=rec))
        naming = tmp_path / "out" / rec.session_id / "speaker_names.json"
        naming.write_text(
            json.dumps({"sessionId": rec.session_id, "mappings": {"spk_0": "田中", "spk_1": "佐藤"}}),
            encoding="utf-8",
        )
        meeting_info = tmp_path / "out" / rec.session_id / "meeting_info.json"
        meeting_info.write_text(json.dumps({"title": "定例会"}), encoding="utf-8")
        pipeline.run(PipelineOptions(input=rec))  # meeting_info は minutes より先に存在
        assert counter["calls"] == 1

        pipeline.run(PipelineOptions(input=rec))  # 再実行しても再要約しない（再利用）

        assert counter["calls"] == 1


class TestMinutesEditGuard:
    """手編集済み議事録（minutes.meta.json の edited=true）は明示 --force 抜きに上書きしない（②）。"""

    def _complete_once(self, tmp_path: Path, counter: dict[str, int]):
        pipeline, s3, transcribe = _build(tmp_path, counter)
        rec = resolve_single(tmp_path / "meeting.wav", "ja-JP")
        return pipeline, s3, transcribe, rec

    def test_meeting_info_update_after_edit_stops_instead_of_overwriting(
        self, tmp_path: Path, single_wav: Path
    ) -> None:
        counter = {"calls": 0}
        pipeline, _s3, _tr = _build(tmp_path, counter)
        rec = resolve_single(single_wav, "ja-JP")
        pipeline.run(PipelineOptions(input=rec))
        naming = tmp_path / "out" / rec.session_id / "speaker_names.json"
        naming.write_text(
            json.dumps({"sessionId": rec.session_id, "mappings": {"spk_0": "田中", "spk_1": "佐藤"}}),
            encoding="utf-8",
        )
        pipeline.run(PipelineOptions(input=rec))  # complete
        assert counter["calls"] == 1

        meta = tmp_path / "out" / rec.session_id / "minutes.meta.json"
        meta.write_text(json.dumps({"edited": True}), encoding="utf-8")
        meeting_info = tmp_path / "out" / rec.session_id / "meeting_info.json"
        meeting_info.write_text(json.dumps({"title": "定例会"}), encoding="utf-8")

        with pytest.raises(PipelineError, match="手編集済み"):
            pipeline.run(PipelineOptions(input=rec))

        assert counter["calls"] == 1  # 上書きは実行されない

    def test_explicit_force_overrides_edited_guard(self, tmp_path: Path, single_wav: Path) -> None:
        counter = {"calls": 0}
        pipeline, _s3, _tr = _build(tmp_path, counter)
        rec = resolve_single(single_wav, "ja-JP")
        pipeline.run(PipelineOptions(input=rec))
        naming = tmp_path / "out" / rec.session_id / "speaker_names.json"
        naming.write_text(
            json.dumps({"sessionId": rec.session_id, "mappings": {"spk_0": "田中", "spk_1": "佐藤"}}),
            encoding="utf-8",
        )
        pipeline.run(PipelineOptions(input=rec))
        assert counter["calls"] == 1

        meta = tmp_path / "out" / rec.session_id / "minutes.meta.json"
        meta.write_text(json.dumps({"edited": True}), encoding="utf-8")

        result = pipeline.run(PipelineOptions(input=rec, target_stage=Stage.SUMMARIZED))

        assert result.status == ResultStatus.COMPLETED
        assert counter["calls"] == 2  # 明示指定なら上書きされる

    def test_unedited_minutes_are_overwritten_without_force(self, tmp_path: Path, single_wav: Path) -> None:
        """`edited` が立っていなければ、従来どおり自動トリガーで上書きできる（回帰）。"""
        counter = {"calls": 0}
        pipeline, _s3, _tr = _build(tmp_path, counter)
        rec = resolve_single(single_wav, "ja-JP")
        pipeline.run(PipelineOptions(input=rec))
        naming = tmp_path / "out" / rec.session_id / "speaker_names.json"
        naming.write_text(
            json.dumps({"sessionId": rec.session_id, "mappings": {"spk_0": "田中", "spk_1": "佐藤"}}),
            encoding="utf-8",
        )
        pipeline.run(PipelineOptions(input=rec))
        assert counter["calls"] == 1

        meeting_info = tmp_path / "out" / rec.session_id / "meeting_info.json"
        meeting_info.write_text(json.dumps({"title": "定例会"}), encoding="utf-8")

        result = pipeline.run(PipelineOptions(input=rec))

        assert result.status == ResultStatus.COMPLETED
        assert counter["calls"] == 2


class TestMaterials:
    """③付帯資料。materials/ の抽出結果を要約プロンプトへ渡す経路と再課金トリガーを検証する。"""

    def _build_capturing(self, tmp_path: Path):
        """summarize 呼び出しの kwargs を記録するフェイク（materials_text の伝播を確認するため）。"""
        calls: list[dict] = []
        s3 = FakeS3Io(others_transcribe_json())
        transcribe = FakeTranscribe()

        def _summarize(transcript: FinalTranscript, config: PipelineConfig, **kwargs: object) -> MinutesDoc:
            calls.append(kwargs)
            return MinutesDoc(
                session_id=transcript.session_id,
                markdown="## 決定事項\n- なし\n## ToDo\n## 論点・議論サマリ",
                source_model="fake-model",
                generated_at_utc=datetime(2026, 6, 20, tzinfo=timezone.utc),
            )

        pipeline = PostMeetingPipeline(
            _config(tmp_path / "out"),
            s3io=s3,
            transcribe_client=transcribe,
            summarizer=_summarize,
            credential_checker=lambda: None,
        )
        return pipeline, calls

    def _complete_naming(self, tmp_path: Path, pipeline, rec) -> None:
        pipeline.run(PipelineOptions(input=rec))  # naming 停止
        naming = tmp_path / "out" / rec.session_id / "speaker_names.json"
        naming.write_text(
            json.dumps({"sessionId": rec.session_id, "mappings": {"spk_0": "田中", "spk_1": "佐藤"}}),
            encoding="utf-8",
        )

    def test_materials_text_is_passed_to_summarizer(self, tmp_path: Path, single_wav: Path) -> None:
        pipeline, calls = self._build_capturing(tmp_path)
        rec = resolve_single(single_wav, "ja-JP")
        self._complete_naming(tmp_path, pipeline, rec)

        materials_dir = tmp_path / "out" / rec.session_id / "materials"
        materials_dir.mkdir(parents=True)
        (materials_dir / "agenda.txt").write_text("会議の前提事項", encoding="utf-8")

        result = pipeline.run(PipelineOptions(input=rec))

        assert result.status == ResultStatus.COMPLETED
        assert len(calls) == 1
        assert "会議の前提事項" in calls[0]["materials_text"]
        assert "agenda.txt" in calls[0]["materials_text"]
        cache = json.loads((tmp_path / "out" / rec.session_id / "materials.extracted.json").read_text(encoding="utf-8"))
        assert cache["materials"][0]["fileName"] == "agenda.txt"

    def test_no_materials_option_omits_reference_block(self, tmp_path: Path, single_wav: Path) -> None:
        pipeline, calls = self._build_capturing(tmp_path)
        rec = resolve_single(single_wav, "ja-JP")
        self._complete_naming(tmp_path, pipeline, rec)

        materials_dir = tmp_path / "out" / rec.session_id / "materials"
        materials_dir.mkdir(parents=True)
        (materials_dir / "agenda.txt").write_text("会議の前提事項", encoding="utf-8")

        result = pipeline.run(PipelineOptions(input=rec, materials=False))

        assert result.status == ResultStatus.COMPLETED
        assert calls[0]["materials_text"] == ""
        # --no-materials では抽出そのものを行わない（キャッシュも作らない）。
        assert not (tmp_path / "out" / rec.session_id / "materials.extracted.json").is_file()

    def test_adding_materials_after_minutes_done_triggers_resummarize_only(
        self, tmp_path: Path, single_wav: Path
    ) -> None:
        pipeline, calls = self._build_capturing(tmp_path)
        rec = resolve_single(single_wav, "ja-JP")
        self._complete_naming(tmp_path, pipeline, rec)
        pipeline.run(PipelineOptions(input=rec))  # complete（資料なし）
        assert len(calls) == 1
        assert calls[0]["materials_text"] == ""

        materials_dir = tmp_path / "out" / rec.session_id / "materials"
        materials_dir.mkdir(parents=True)
        (materials_dir / "agenda.txt").write_text("追加された資料", encoding="utf-8")

        result = pipeline.run(PipelineOptions(input=rec))

        assert result.status == ResultStatus.COMPLETED
        assert len(calls) == 2  # summarize のみ再実行
        assert "追加された資料" in calls[1]["materials_text"]

    def test_unchanged_materials_does_not_retrigger_summarize(self, tmp_path: Path, single_wav: Path) -> None:
        pipeline, calls = self._build_capturing(tmp_path)
        rec = resolve_single(single_wav, "ja-JP")
        self._complete_naming(tmp_path, pipeline, rec)

        materials_dir = tmp_path / "out" / rec.session_id / "materials"
        materials_dir.mkdir(parents=True)
        (materials_dir / "agenda.txt").write_text("資料本文", encoding="utf-8")

        pipeline.run(PipelineOptions(input=rec))  # complete
        assert len(calls) == 1

        result = pipeline.run(PipelineOptions(input=rec))  # 資料は変わっていない

        assert result.status == ResultStatus.COMPLETED
        assert len(calls) == 1  # 再要約されない（再利用）

    def test_session_without_materials_dir_is_unaffected(self, tmp_path: Path, single_wav: Path) -> None:
        """付帯資料を一度も使っていないセッションは、materials 判定で無駄な再要約を起こさない。"""
        pipeline, calls = self._build_capturing(tmp_path)
        rec = resolve_single(single_wav, "ja-JP")
        self._complete_naming(tmp_path, pipeline, rec)

        pipeline.run(PipelineOptions(input=rec))
        assert len(calls) == 1

        result = pipeline.run(PipelineOptions(input=rec))

        assert result.status == ResultStatus.COMPLETED
        assert len(calls) == 1
