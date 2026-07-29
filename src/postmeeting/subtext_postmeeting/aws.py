"""boto3 クライアント生成の共通ヘルパ（遅延 import）。

認証情報はコードに持たず、AWS 既定のクレデンシャルプロバイダチェーン（プロファイル/SSO/
環境変数）に委譲する（NFR-SEC-07）。boto3 は遅延 import とし、テストはクライアント注入で
本ヘルパを迂回する。import 失敗は握り潰さず、どの段で起きたかを付けた PipelineError に倒す。
"""

from __future__ import annotations

from typing import Any

from .errors import PipelineError


def import_boto3(stage: str) -> Any:
    """boto3 モジュールを遅延 import して返す。未導入は actionable な PipelineError。

    クライアント生成失敗を呼び出し側で分類したい場合（例: STS 認証チェック）は、本関数で
    import だけ済ませてから `boto3.client(...)` を各自の try/except で囲む。
    """
    try:
        import boto3  # 遅延 import（テストでは client 注入で回避）
    except ImportError as exc:  # pragma: no cover
        raise PipelineError("boto3 が見つかりません。`uv sync` を実行してください。", failed_stage=stage) from exc
    return boto3


def build_client(service: str, region: str, *, stage: str) -> Any:
    """指定サービスの boto3 クライアントを生成する（HTTPS 既定＝TLS）。"""
    return import_boto3(stage).client(service, region_name=region)
