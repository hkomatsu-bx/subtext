"""S3 入出力・後始末（L2/L8 / BR-IO）。

WAV を処理用プレフィックスへアップロードし（SSE-S3 前提・TLS）、Transcribe 出力を取得、成功後に
keepS3=false なら削除する（BR-IO-02）。バケットの暗号化・公開ブロック前提を起動時に確認し、未充足
なら安全側に停止する（BR-IO-01, NFR-SEC-01/03）。boto3 は HTTPS 既定のため転送は TLS（BR-IO-04）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .aws import build_client
from .errors import PipelineError

_STAGE = "s3"
# S3 キー / Transcribe ジョブ名に使える ASCII 集合以外。TranscriptionJobName は `[0-9a-zA-Z._-]`
# 限定（日本語不可）で、OutputKey も同様に制限される。
_UNSAFE_KEY_CHARS = re.compile(r"[^0-9A-Za-z._-]+")
_REPEATED_DASH = re.compile(r"-{2,}")
# キーの1セグメントの最大長（ジョブ名の上限に合わせた保守的な値）。
_MAX_SEGMENT_LEN = 80


def safe_key_segment(raw: str) -> str:
    """S3 キー / Transcribe ジョブ名に安全な ASCII セグメントへ変換する（純粋）。

    セッションIDは入力ファイル名由来（single / mp4 経路）で任意文字列になり得る。生のまま
    キーに使うと、**WAV をアップロードした後に** ジョブ開始が ValidationException で落ちる
    （課金前だが無駄なアップロードと不透明なエラーになる）。非 ASCII・記号・空白は `-` に畳み、
    連続 `-` を1つに圧縮する。結果が空なら 'session' を返す。
    """
    cleaned = _UNSAFE_KEY_CHARS.sub("-", raw)
    cleaned = _REPEATED_DASH.sub("-", cleaned).strip("-.")[:_MAX_SEGMENT_LEN]
    return cleaned or "session"


class S3Io:
    """対象バケットに限定した S3 操作のラッパ。client は注入可能（テスト容易性）。"""

    def __init__(self, bucket: str, region: str, client: Any | None = None) -> None:
        self._bucket = bucket
        self._region = region
        self._client = client if client is not None else build_client("s3", region, stage=_STAGE)

    @property
    def bucket(self) -> str:
        return self._bucket

    def verify_preconditions(self) -> None:
        """暗号化・パブリックアクセスブロックの前提を確認する（BR-IO-01）。"""
        self._assert_encryption_enabled()
        self._assert_public_access_blocked()

    def upload(self, local_path: Path, key: str) -> str:
        """ローカル WAV をアップロードし s3:// URI を返す。"""
        try:
            self._client.upload_file(str(local_path), self._bucket, key)
        except Exception as exc:  # boto3 ClientError 等を文脈付きで送出（BR-ERR-01）
            raise PipelineError(
                f"S3 アップロードに失敗しました: s3://{self._bucket}/{key}", failed_stage=_STAGE
            ) from exc
        return f"s3://{self._bucket}/{key}"

    def download_json(self, key: str) -> dict[str, Any]:
        """S3 上の JSON（Transcribe 出力）を取得して dict で返す。"""
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            body = response["Body"].read()
        except Exception as exc:
            raise PipelineError(f"S3 取得に失敗しました: s3://{self._bucket}/{key}", failed_stage=_STAGE) from exc
        try:
            data: dict[str, Any] = json.loads(body)
        except (json.JSONDecodeError, TypeError) as exc:
            raise PipelineError(f"S3 オブジェクトの JSON 解析に失敗しました: {key}", failed_stage=_STAGE) from exc
        return data

    def delete_prefix(self, prefix: str) -> int:
        """指定プレフィックス配下を列挙して一括削除し、削除件数を返す（ISS-13 / BR-H2-S3-01）。

        対象は与えられた prefix（例 `subtext/jobs/<session_id>/`）配下のみに厳密限定する。
        冪等（対象ゼロでも成功）。フェイルセーフは呼び出し側（pipeline._cleanup）が担保するため、
        ここでは失敗を文脈付きで送出する（BR-ERR-01）。後始末失敗時はライフサイクル(7日)が保険。
        """
        deleted = 0
        failures: list[dict[str, Any]] = []
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
                if not objects:
                    continue  # 空ページはスキップ（ガード節で本体を1段浅くする）。
                response = self._client.delete_objects(Bucket=self._bucket, Delete={"Objects": objects})
                # DeleteObjects は **HTTP 200 でもキー単位で失敗する**（応答の Errors 配列）。応答を見ずに
                # 要求件数を数えると、権限不足やオブジェクトロックで消えていない PII 音声を「削除した」と
                # 報告してしまう（NFR-SEC-04 の後始末が効いていないのに効いた体になる）。
                deleted += len(response.get("Deleted") or [])
                failures.extend(response.get("Errors") or [])
        except Exception as exc:
            raise PipelineError(
                f"S3 プレフィックス削除に失敗しました: s3://{self._bucket}/{prefix}",
                failed_stage=_STAGE,
            ) from exc

        if failures:
            # キー名はメッセージに載せない（BR-ERR-04）。原因コードだけを添える。
            codes = ",".join(sorted({str(f.get("Code", "Unknown")) for f in failures}))
            raise PipelineError(
                f"S3 オブジェクトを削除できませんでした（{len(failures)} 件・code={codes}）: "
                f"s3://{self._bucket}/{prefix}",
                failed_stage=_STAGE,
            )
        return deleted

    # ------------------------------------------------------------------
    def _assert_encryption_enabled(self) -> None:
        try:
            self._client.get_bucket_encryption(Bucket=self._bucket)
        except Exception as exc:
            raise PipelineError(
                f"バケット '{self._bucket}' の保存時暗号化が確認できません（NFR-SEC-01）。",
                failed_stage=_STAGE,
            ) from exc

    def _assert_public_access_blocked(self) -> None:
        try:
            config = self._client.get_public_access_block(Bucket=self._bucket)
        except Exception as exc:
            raise PipelineError(
                f"バケット '{self._bucket}' のパブリックアクセスブロックが確認できません（NFR-SEC-03）。",
                failed_stage=_STAGE,
            ) from exc
        block = config.get("PublicAccessBlockConfiguration", {})
        if not all(
            block.get(flag, False)
            for flag in (
                "BlockPublicAcls",
                "IgnorePublicAcls",
                "BlockPublicPolicy",
                "RestrictPublicBuckets",
            )
        ):
            raise PipelineError(
                f"バケット '{self._bucket}' のパブリックアクセスブロックが不完全です（NFR-SEC-03）。",
                failed_stage=_STAGE,
            )
