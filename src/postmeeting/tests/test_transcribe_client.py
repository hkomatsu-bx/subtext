"""Transcribe ジョブ仕様・起動のテスト（C1 配線 = FR-C1-02・NFR-C1-04）。

AWS には触れず、make_job_spec の語彙反映と _start が Settings に VocabularyName を
載せる/載せないを fake client の捕捉 kwargs で検証する。
"""

from __future__ import annotations

from typing import Any

import pytest

from subtext_postmeeting.errors import PipelineError
from subtext_postmeeting.models import StreamRole
from subtext_postmeeting.transcribe_client import TranscribeClient, make_job_spec


class _FakeClient:
    """start_transcription_job の kwargs を捕捉し、get_* は即 COMPLETED を返す fake。"""

    def __init__(self) -> None:
        self.start_kwargs: dict[str, Any] = {}

    def start_transcription_job(self, **kwargs: Any) -> None:
        self.start_kwargs = kwargs

    def get_transcription_job(self, TranscriptionJobName: str) -> dict[str, Any]:  # noqa: N803
        return {"TranscriptionJob": {"TranscriptionJobStatus": "COMPLETED"}}


def _run(spec) -> _FakeClient:  # type: ignore[no-untyped-def]
    client = _FakeClient()
    tc = TranscribeClient(region="ap-northeast-1", client=client, sleeper=lambda _: None)
    tc.run_job(spec, output_bucket="b", output_key="k.json", poll_timeout_sec=60)
    return client


# --- make_job_spec ----------------------------------------------------------
def test_make_job_spec_carries_vocabulary_name() -> None:
    spec = make_job_spec("sess-1", StreamRole.SELF, "s3://b/self.wav", "ja-JP", 5, "subtext-ja")
    assert spec.vocabulary_name == "subtext-ja"


def test_make_job_spec_empty_vocabulary_name_becomes_none() -> None:
    spec = make_job_spec("sess-1", StreamRole.SELF, "s3://b/self.wav", "ja-JP", 5, "")
    assert spec.vocabulary_name is None


def test_make_job_spec_omitted_vocabulary_name_is_none() -> None:
    spec = make_job_spec("sess-1", StreamRole.OTHERS, "s3://b/others.wav", "ja-JP", 5)
    assert spec.vocabulary_name is None


# --- _start が Settings.VocabularyName を載せる/載せない ---------------------
def test_start_includes_vocabulary_name_when_set() -> None:
    spec = make_job_spec("sess-1", StreamRole.SELF, "s3://b/self.wav", "ja-JP", 5, "subtext-ja")
    client = _run(spec)
    assert client.start_kwargs["Settings"]["VocabularyName"] == "subtext-ja"


def test_start_omits_vocabulary_name_when_unset() -> None:
    spec = make_job_spec("sess-1", StreamRole.SELF, "s3://b/self.wav", "ja-JP", 5)
    client = _run(spec)
    assert "VocabularyName" not in client.start_kwargs["Settings"]


def test_start_others_includes_speaker_labels_and_vocabulary() -> None:
    spec = make_job_spec("sess-1", StreamRole.OTHERS, "s3://b/others.wav", "ja-JP", 5, "subtext-ja")
    client = _run(spec)
    settings = client.start_kwargs["Settings"]
    assert settings["ShowSpeakerLabels"] is True
    assert settings["MaxSpeakerLabels"] == 5
    assert settings["VocabularyName"] == "subtext-ja"


# --- media_format / FAILED 理由の surface ------------------------------------
def test_start_uses_media_format_from_spec() -> None:
    spec = make_job_spec("sess-1", StreamRole.OTHERS, "s3://b/rec.mp4", "ja-JP", 5, media_format="mp4")
    client = _run(spec)
    assert client.start_kwargs["MediaFormat"] == "mp4"


def test_make_job_spec_ascii_sanitizes_japanese_session_id() -> None:
    # 日本語 session_id でもジョブ名は ASCII（[0-9a-zA-Z._-]）のみになる。
    spec = make_job_spec("【サンプルJP様】定例会", StreamRole.OTHERS, "s3://b/x.mp4", "ja-JP", 5)
    import re

    assert re.fullmatch(r"[0-9A-Za-z._-]+", spec.job_name)


class _FailingBoto:
    def start_transcription_job(self, **kwargs: Any) -> None:
        pass

    def get_transcription_job(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "TranscriptionJob": {
                "TranscriptionJobStatus": "FAILED",
                "FailureReason": "The media format that you specified doesn't match.",
            }
        }


def test_failed_job_surfaces_failure_reason() -> None:
    tc = TranscribeClient(region="ap-northeast-1", client=_FailingBoto(), sleeper=lambda _s: None)
    spec = make_job_spec("s", StreamRole.OTHERS, "s3://b/x.mp4", "ja-JP", 5, media_format="mp4")
    with pytest.raises(PipelineError) as ei:
        tc.run_job(spec, "bucket", "out.json", 60)
    assert "match" in str(ei.value)  # FailureReason がメッセージに出る
