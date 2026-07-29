"""Transcribe バッチ起動・ポーリング（L3 / BR-JOB）。

self は ShowSpeakerLabels=false、others は true（MaxSpeakerLabels=設定値, 既定5）で別ジョブを起動
する（Q1=A, BR-JOB-01/02）。jobName は一意化し再実行で衝突しない（BR-JOB-04）。完了は指数バック
オフ＋上限 pollTimeoutSec で待ち、FAILED/timeout は PipelineError として安全側停止（BR-JOB-05）。
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable

from .aws import build_client
from .errors import PipelineError
from .models import StreamRole, TranscribeJobSpec

_STAGE = "transcribe"
_INITIAL_BACKOFF_SEC = 5.0
_MAX_BACKOFF_SEC = 30.0


def make_job_spec(
    session_id: str,
    role: StreamRole,
    media_s3_uri: str,
    language: str,
    max_speakers: int,
    vocabulary_name: str | None = None,
    media_format: str = "wav",
) -> TranscribeJobSpec:
    """系統に応じた Transcribe ジョブ仕様を作る。jobName は uuid で一意化（BR-JOB-04）。

    vocabulary_name は空/None なら未設定（カスタム語彙なし＝従来動作, C1・FR-C1-02）。
    media_format は録音経路 "wav" 固定／mp4→VTT ツールは ffmpeg で抽出した "flac"
    （Teams の mp4 は Transcribe が直接 parse できないため FLAC 抽出を挟む, FR-18）。
    """
    show_labels = role == StreamRole.OTHERS
    # TranscriptionJobName は ASCII（[0-9a-zA-Z._-]）限定。isalnum() は日本語も真を返すため
    # ASCII 判定を併用する（非 ASCII の session_id でジョブ起動が弾かれるのを防ぐ）。
    safe_session = "".join(c if (c.isascii() and c.isalnum()) or c in "-_" else "-" for c in session_id)[:80]
    job_name = f"subtext-{safe_session}-{role.value}-{uuid.uuid4().hex[:8]}"
    return TranscribeJobSpec(
        job_name=job_name,
        role=role,
        media_s3_uri=media_s3_uri,
        language_code=language,
        show_speaker_labels=show_labels,
        max_speaker_labels=max_speakers if show_labels else None,
        vocabulary_name=vocabulary_name or None,  # "" → None（未設定）
        media_format=media_format,
    )


class TranscribeClient:
    """Transcribe バッチ操作のラッパ。client/sleeper/monotonic は注入可能（テスト容易性）。"""

    def __init__(
        self,
        region: str,
        client: Any | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._region = region
        self._client = client if client is not None else build_client("transcribe", region, stage=_STAGE)
        self._sleep = sleeper
        self._monotonic = monotonic

    def run_job(
        self,
        spec: TranscribeJobSpec,
        output_bucket: str,
        output_key: str,
        poll_timeout_sec: int,
    ) -> str:
        """ジョブを起動し完了まで待つ。成功時に出力 S3 キー（output_key）を返す。"""
        self._start(spec, output_bucket, output_key)
        self._poll_until_done(spec.job_name, poll_timeout_sec)
        return output_key

    def _start(self, spec: TranscribeJobSpec, output_bucket: str, output_key: str) -> None:
        settings: dict[str, Any] = {"ShowSpeakerLabels": spec.show_speaker_labels}
        if spec.show_speaker_labels and spec.max_speaker_labels is not None:
            settings["MaxSpeakerLabels"] = spec.max_speaker_labels
        if spec.vocabulary_name:  # C1・FR-C1-02: 設定時のみカスタム語彙を適用
            settings["VocabularyName"] = spec.vocabulary_name
        try:
            self._client.start_transcription_job(
                TranscriptionJobName=spec.job_name,
                LanguageCode=spec.language_code,
                MediaFormat=spec.media_format,
                Media={"MediaFileUri": spec.media_s3_uri},
                OutputBucketName=output_bucket,
                OutputKey=output_key,
                Settings=settings,
            )
        except Exception as exc:
            # 起因（AWS エラーコード等）をメッセージに出す。job 名は ASCII 化済で PII を含まない。
            raise PipelineError(
                f"Transcribe ジョブ起動に失敗しました（role={spec.role.value}）: {exc}",
                failed_stage=_STAGE,
            ) from exc

    def _poll_until_done(self, job_name: str, poll_timeout_sec: int) -> None:
        deadline = self._monotonic() + poll_timeout_sec
        backoff = _INITIAL_BACKOFF_SEC
        while True:
            job = self._get_job(job_name)
            status = str(job.get("TranscriptionJobStatus", ""))
            if status == "COMPLETED":
                return
            if status == "FAILED":
                # FailureReason を surface（AWS 技術メッセージ・PII 非含有）。切り分けに必須。
                reason = str(job.get("FailureReason") or "理由不明")
                raise PipelineError(
                    f"Transcribe ジョブが FAILED で終了しました（job={job_name}）: {reason}",
                    failed_stage=_STAGE,
                )
            if self._monotonic() >= deadline:
                raise PipelineError(
                    f"Transcribe ジョブがタイムアウトしました（{poll_timeout_sec}秒, job={job_name}）。",
                    failed_stage=_STAGE,
                )
            self._sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF_SEC)

    def _get_job(self, job_name: str) -> dict[str, Any]:
        try:
            response = self._client.get_transcription_job(TranscriptionJobName=job_name)
        except Exception as exc:
            raise PipelineError(
                f"Transcribe ジョブ状態の取得に失敗しました（job={job_name}）。",
                failed_stage=_STAGE,
            ) from exc
        return dict(response.get("TranscriptionJob", {}))
