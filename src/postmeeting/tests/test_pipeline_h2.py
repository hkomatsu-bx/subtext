"""H2 で追加した事前認証チェックと S3 cleanup 全経路対応のテスト（FR-H2-07 / FR-H2-10・ISS-13）。

AWS 境界はフェイク化し、AWS 非依存で検証する（NFR-H2-03）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from subtext_postmeeting.config import PipelineConfig
from subtext_postmeeting.errors import PipelineError
from subtext_postmeeting.input_resolver import resolve_single
from subtext_postmeeting.models import FinalTranscript, MinutesDoc
from subtext_postmeeting.pipeline import PipelineOptions, PostMeetingPipeline, ResultStatus

from .conftest import others_transcribe_json


class FakeS3Io:
    """delete_prefix の挙動を制御できる S3Io フェイク。"""

    def __init__(self, transcribe_json: dict[str, Any], *, fail_delete: bool = False) -> None:
        self.bucket = "fake-bucket"
        self._transcribe_json = transcribe_json
        self._fail_delete = fail_delete
        self.uploaded: list[str] = []
        self.deleted_prefixes: list[str] = []

    def verify_preconditions(self) -> None:
        pass

    def upload(self, local_path: Path, key: str) -> str:
        self.uploaded.append(key)
        return f"s3://{self.bucket}/{key}"

    def download_json(self, key: str) -> dict[str, Any]:
        return self._transcribe_json

    def delete_prefix(self, prefix: str) -> int:
        if self._fail_delete:
            raise PipelineError("削除失敗（テスト）", failed_stage="s3")
        self.deleted_prefixes.append(prefix)
        return 1


class FakeTranscribe:
    def __init__(self) -> None:
        self.calls = 0

    def run_job(self, spec: Any, bucket: str, output_key: str, timeout: int) -> str:
        self.calls += 1
        return output_key


def _fake_summarizer(transcript: FinalTranscript, config: PipelineConfig) -> MinutesDoc:
    return MinutesDoc(
        session_id=transcript.session_id,
        markdown="## 決定事項\n- なし\n## ToDo\n## 論点・議論サマリ",
        source_model="fake-model",
        generated_at_utc=datetime(2026, 6, 25, tzinfo=timezone.utc),
    )


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
def single_wav(make_wav) -> Path:
    return make_wav("meeting.wav")


def _fill_names(out_dir: Path, session_id: str) -> None:
    naming = out_dir / session_id / "speaker_names.json"
    naming.write_text(
        json.dumps({"sessionId": session_id, "mappings": {"spk_0": "田中", "spk_1": "佐藤"}}),
        encoding="utf-8",
    )


def _build(tmp_path: Path, checker, *, fail_delete: bool = False):
    s3 = FakeS3Io(others_transcribe_json(), fail_delete=fail_delete)
    transcribe = FakeTranscribe()
    pipeline = PostMeetingPipeline(
        _config(tmp_path / "out"),
        s3io=s3,
        transcribe_client=transcribe,
        summarizer=_fake_summarizer,
        credential_checker=checker,
    )
    return pipeline, s3, transcribe


class TestPreflight:
    def test_preflight_runs_before_charge_on_first_run(self, tmp_path: Path, single_wav: Path) -> None:
        calls = {"n": 0}
        pipeline, _s3, transcribe = _build(tmp_path, lambda: calls.__setitem__("n", calls["n"] + 1))
        rec = resolve_single(single_wav, "ja-JP")
        result = pipeline.run(PipelineOptions(input=rec))
        assert result.status == ResultStatus.NAMING_REQUIRED
        assert calls["n"] == 1
        assert transcribe.calls == 1  # 課金段は実行された

    def test_preflight_failure_prevents_charge(self, tmp_path: Path, single_wav: Path) -> None:
        def _boom() -> None:
            raise PipelineError("認証無効（テスト）", failed_stage="auth")

        pipeline, s3, transcribe = _build(tmp_path, _boom)
        rec = resolve_single(single_wav, "ja-JP")
        with pytest.raises(PipelineError) as exc:
            pipeline.run(PipelineOptions(input=rec))
        assert exc.value.failed_stage == "auth"
        assert transcribe.calls == 0  # 課金前に停止
        assert s3.uploaded == []  # アップロードもしていない

    def test_client_build_credential_error_is_actionable(self, monkeypatch) -> None:
        # boto3.client 生成時の資格情報解決エラー（ProfileNotFound 等）を生トレースバックでなく
        # actionable な auth PipelineError へ変換する（実 AWS で発見した Finding B の回帰防止）。
        import boto3

        from subtext_postmeeting import pipeline as pl

        class _ProfileNotFound(Exception):
            pass

        _ProfileNotFound.__name__ = "ProfileNotFound"

        def _raise(*args: Any, **kwargs: Any) -> Any:
            raise _ProfileNotFound("config profile could not be found")

        monkeypatch.setattr(boto3, "client", _raise)
        checker = pl.make_credential_checker("ap-northeast-1")
        with pytest.raises(PipelineError) as exc:
            checker()
        assert exc.value.failed_stage == "auth"
        # プロファイル不在は再ログインでは直らないため、綴りの確認を促す文言になる
        # （`--profile` 追加でプロファイル名の打ち間違いが起こり得るようになったため分けた）。
        assert "AWS_PROFILE" in str(exc.value)
        assert "aws login" not in str(exc.value)

    def test_preflight_skipped_on_full_reuse(self, tmp_path: Path, single_wav: Path) -> None:
        calls = {"n": 0}
        pipeline, _s3, _tr = _build(tmp_path, lambda: calls.__setitem__("n", calls["n"] + 1))
        rec = resolve_single(single_wav, "ja-JP")
        pipeline.run(PipelineOptions(input=rec))  # naming 停止（preflight 1）
        _fill_names(tmp_path / "out", rec.session_id)
        pipeline.run(PipelineOptions(input=rec))  # 完走（preflight 2）
        assert calls["n"] == 2
        # 全段再利用の再実行では課金が無いため preflight は走らない
        result = pipeline.run(PipelineOptions(input=rec))
        assert result.status == ResultStatus.COMPLETED
        assert calls["n"] == 2


class TestCleanupAllPaths:
    def _prefix(self, session_id: str) -> str:
        return f"subtext/jobs/{session_id}/"

    def test_cleanup_on_naming_stop(self, tmp_path: Path, single_wav: Path) -> None:
        pipeline, s3, _tr = _build(tmp_path, lambda: None)
        rec = resolve_single(single_wav, "ja-JP")
        pipeline.run(PipelineOptions(input=rec))  # naming 停止
        assert self._prefix(rec.session_id) in s3.deleted_prefixes

    def test_cleanup_on_reuse_path(self, tmp_path: Path, single_wav: Path) -> None:
        pipeline, s3, _tr = _build(tmp_path, lambda: None)
        rec = resolve_single(single_wav, "ja-JP")
        pipeline.run(PipelineOptions(input=rec))
        _fill_names(tmp_path / "out", rec.session_id)
        pipeline.run(PipelineOptions(input=rec))  # 完走
        s3.deleted_prefixes.clear()
        pipeline.run(PipelineOptions(input=rec))  # 全段再利用でも cleanup は走る（ISS-13）
        assert self._prefix(rec.session_id) in s3.deleted_prefixes

    def test_keep_s3_skips_cleanup(self, tmp_path: Path, single_wav: Path) -> None:
        pipeline, s3, _tr = _build(tmp_path, lambda: None)
        rec = resolve_single(single_wav, "ja-JP")
        pipeline.run(PipelineOptions(input=rec, keep_s3=True))  # naming 停止
        assert s3.deleted_prefixes == []

    def test_cleanup_on_transcribe_failure(self, tmp_path: Path, single_wav: Path) -> None:
        # mid-pipeline 例外（Transcribe 失敗）でも cleanup が走り S3 残置を残さない
        # （ISS-13 の例外経路。実 AWS 受け入れ AC-H2-08 で発見した欠陥の回帰防止）。
        class _FailingTranscribe:
            def run_job(self, spec: Any, bucket: str, output_key: str, timeout: int) -> str:
                raise PipelineError("Transcribe ジョブが FAILED（テスト）", failed_stage="transcribe")

        s3 = FakeS3Io(others_transcribe_json())
        pipeline = PostMeetingPipeline(
            _config(tmp_path / "out"),
            s3io=s3,
            transcribe_client=_FailingTranscribe(),
            summarizer=_fake_summarizer,
            credential_checker=lambda: None,
        )
        rec = resolve_single(single_wav, "ja-JP")
        with pytest.raises(PipelineError) as exc:
            pipeline.run(PipelineOptions(input=rec))
        assert exc.value.failed_stage == "transcribe"
        assert s3.uploaded  # 入力 WAV はアップロード済み（＝残置の元）
        # 例外でも当該 session プレフィックスが掃除されている
        assert self._prefix(rec.session_id) in s3.deleted_prefixes

    def test_cleanup_failure_is_failsafe(self, tmp_path: Path, single_wav: Path) -> None:
        # delete_prefix が失敗しても完走し minutes は生成される（BR-H2-S3-01）。
        pipeline, _s3, _tr = _build(tmp_path, lambda: None, fail_delete=True)
        rec = resolve_single(single_wav, "ja-JP")
        pipeline.run(PipelineOptions(input=rec))  # naming 停止（cleanup 失敗を握る）
        _fill_names(tmp_path / "out", rec.session_id)
        result = pipeline.run(PipelineOptions(input=rec))
        assert result.status == ResultStatus.COMPLETED
        assert result.minutes_path is not None and result.minutes_path.is_file()
