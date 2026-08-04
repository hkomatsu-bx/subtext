"""boto3 クライアント生成の共通ヘルパ（遅延 import）。

認証情報はコードに持たず、AWS 既定のクレデンシャルプロバイダチェーン（プロファイル/SSO/
環境変数）に委譲する（NFR-SEC-07）。boto3 は遅延 import とし、テストはクライアント注入で
本ヘルパを迂回する。import 失敗は握り潰さず、どの段で起きたかを付けた PipelineError に倒す。
"""

from __future__ import annotations

import os
from typing import Any

from .errors import PipelineError

# AWS プロファイルを選ぶ環境変数（SDK 共通。Unit C の .NET SDK も同じ変数を見る）。
_PROFILE_ENV = "AWS_PROFILE"


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


def resolve_profile(cli_profile: str | None, env_profile: str) -> str:
    """使う AWS プロファイルを決める（純粋）。`--profile` > 環境変数 > `.env` > 未指定（空）。

    環境変数と `.env` の優先関係は `PipelineConfig.from_env`（`_load_env`）が解決済みで、
    その結果が `env_profile` に入る。空文字は「未指定」を意味する。

    `--profile` は**先に空白を落としてから**判定する。`--profile " "` のような値を真と見なすと、
    環境変数や `.env` の指定を打ち消して黙って SDK 既定（別アカウント）へ倒れる。
    """
    return (cli_profile or "").strip() or env_profile.strip()


def apply_profile(profile: str) -> None:
    """使う AWS プロファイルを環境変数へ書き出す（エントリポイントで 1 回だけ呼ぶ）。

    boto3 クライアント生成は `build_client` と `auth_check` の 2 か所だが、その呼び出し元は
    4 か所あり、`S3Io`／`TranscribeClient` のコンストラクタ（region しか受けていない）まで
    引数が波及する。プロファイル選択はプロセス全体の設定なので、境界で環境へ渡す。

    **空文字なら何も設定しない。** `default` を明示的に入れてはならない。botocore は
    プロファイルが明示されていると `~/.aws/config` に該当セクションが無い場合 `ProfileNotFound`
    を送出する（未設定なら空の設定を返して許容する）。`[default]` を持たない環境（資格情報を
    環境変数で渡す運用・CI）で、今まで動いていたものが壊れる。未設定でも SDK は既定で
    default プロファイルを使うため、「未指定なら default」は何もしないことで満たされる。
    """
    if profile:
        os.environ[_PROFILE_ENV] = profile
