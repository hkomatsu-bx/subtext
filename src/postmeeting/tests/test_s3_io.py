"""S3Io.delete_prefix のテスト（FR-H2-10 / BR-H2-S3-01・ISS-13）。

boto3 クライアントをフェイク注入し、プレフィックス限定の一括削除・冪等・エラー送出を検証する。
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from subtext_postmeeting.errors import PipelineError
from subtext_postmeeting.s3_io import S3Io, safe_key_segment


class _FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages
        self.calls: list[dict[str, Any]] = []

    def paginate(self, **kwargs: Any):
        self.calls.append(kwargs)
        yield from self._pages


class _FakeClient:
    """S3 クライアントのフェイク。

    `delete_objects` は **実 API と同じ形の応答**（Deleted / Errors）を返す。`{}` を返す
    フェイクにすると「キー単位の失敗（Errors）を無視して要求件数を削除済みと数える」不具合が
    構造的に検出できない。
    """

    def __init__(
        self,
        pages: list[dict[str, Any]],
        *,
        fail: bool = False,
        error_code: str | None = None,
    ) -> None:
        self._paginator = _FakePaginator(pages)
        self._fail = fail
        self._error_code = error_code
        self.deleted: list[list[str]] = []

    def get_paginator(self, name: str) -> _FakePaginator:
        assert name == "list_objects_v2"
        return self._paginator

    def delete_objects(self, *, Bucket: str, Delete: dict[str, Any]) -> dict[str, Any]:
        if self._fail:
            raise RuntimeError("delete failed")
        keys = [o["Key"] for o in Delete["Objects"]]
        self.deleted.append(keys)
        if self._error_code is not None:
            return {"Errors": [{"Key": k, "Code": self._error_code} for k in keys]}
        return {"Deleted": [{"Key": k} for k in keys]}


def _s3(client: _FakeClient) -> S3Io:
    return S3Io("fake-bucket", "ap-northeast-1", client=client)


def test_delete_prefix_deletes_listed_objects() -> None:
    pages = [
        {"Contents": [{"Key": "subtext/jobs/s1/self.wav"}, {"Key": "subtext/jobs/s1/others.wav"}]},
        {"Contents": [{"Key": "subtext/jobs/s1/transcribe/others.json"}]},
    ]
    client = _FakeClient(pages)
    deleted = _s3(client).delete_prefix("subtext/jobs/s1/")
    assert deleted == 3
    # プレフィックスで列挙していること
    assert client._paginator.calls[0]["Prefix"] == "subtext/jobs/s1/"
    flat = [k for batch in client.deleted for k in batch]
    assert flat == [
        "subtext/jobs/s1/self.wav",
        "subtext/jobs/s1/others.wav",
        "subtext/jobs/s1/transcribe/others.json",
    ]


def test_delete_prefix_is_idempotent_when_empty() -> None:
    client = _FakeClient([{"Contents": []}])
    deleted = _s3(client).delete_prefix("subtext/jobs/missing/")
    assert deleted == 0
    assert client.deleted == []  # 削除呼び出しもしない


def test_safe_key_segment_folds_non_ascii() -> None:
    """S3 キー / Transcribe ジョブ名に使える ASCII へ畳むこと。

    single モードのセッションIDは WAV のファイル名（日本語もあり得る）由来。生のままキーに使うと
    **WAV をアップロードした後に** ジョブ開始が ValidationException で落ちる（無駄なアップロードと
    不透明なエラー）。mp4 経路と同じ規則をここへ集約している。
    """
    assert re.fullmatch(r"[0-9A-Za-z._-]+", safe_key_segment("【サンプルJP様】定例会 2026-07-29"))
    assert "--" not in safe_key_segment("会議 -- 記録")
    assert safe_key_segment("会議録") == "session"  # 全て非 ASCII なら既定名


def test_delete_prefix_raises_when_keys_fail_individually() -> None:
    """DeleteObjects は HTTP 200 でもキー単位で失敗する。応答の Errors を見て失敗させること。

    見落とすと、消えていない PII 音声を「削除した」と件数付きでログに残す（後始末が効いている
    ように見えて実際は残置）。呼び出し側（pipeline._cleanup）はフェイルセーフで警告に落とすため、
    ここで送出することが唯一の可視化手段になる。
    """
    pages = [{"Contents": [{"Key": "subtext/jobs/s1/self.wav"}]}]
    client = _FakeClient(pages, error_code="AccessDenied")

    with pytest.raises(PipelineError) as exc:
        _s3(client).delete_prefix("subtext/jobs/s1/")

    assert "AccessDenied" in str(exc.value)
    assert "self.wav" not in str(exc.value)  # キー名は載せない（BR-ERR-04）
    assert exc.value.failed_stage == "s3"


def test_delete_prefix_raises_pipeline_error_on_failure() -> None:
    pages = [{"Contents": [{"Key": "subtext/jobs/s1/self.wav"}]}]
    client = _FakeClient(pages, fail=True)
    with pytest.raises(PipelineError) as exc:
        _s3(client).delete_prefix("subtext/jobs/s1/")
    assert exc.value.failed_stage == "s3"
