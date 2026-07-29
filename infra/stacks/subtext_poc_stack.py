"""Subtext PoC スタック（infrastructure-design.md §2/3/6）。

S3 処理用バケット（SSE-S3 / Block Public Access 全ON / 7日ライフサイクル / TLS強制）と、
Transcribe・Bedrock・対象 S3 に限定した最小権限 IAM（NFR-SEC-01/02/03）をコード化する。
出力（CfnOutput）のバケット名・IAM 主体名を Unit B の .env に転記する。
"""

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct

# Bedrock 呼び出しモデル ID は設定外出し（Q5=A）。context で上書き可能。
# 新しめの Claude はオンデマンド不可・推論プロファイル経由のみのため、既定は
# 日本リージョン内に閉じる（データ残留）jp. システム定義プロファイルを使う。
#
# Sonnet 5 を採らない理由（2026-07-29 確認）: Bedrock の Sonnet 5 は
# `global.anthropic.claude-sonnet-5`（全対応リージョンへルーティング）しか無く、
# `jp.` プロファイルが存在しない。日本国内保管の方針を崩さずに新しいモデルへ上げるため、
# jp. プロファイルがある Opus 4.8 を選んだ。
_DEFAULT_BEDROCK_MODEL_ID = "jp.anthropic.claude-opus-4-8"
# 上記 jp. プロファイルが束ねる基盤モデルとリージョン（get-inference-profile で確認）。
# InvokeModel はプロファイル ARN と各リージョンの foundation-model ARN の双方に許可が要る。
_BEDROCK_FOUNDATION_MODEL = "anthropic.claude-opus-4-8"
_BEDROCK_PROFILE_REGIONS = ("ap-northeast-1", "ap-northeast-3")
_LIFECYCLE_EXPIRATION_DAYS = 7


class SubtextPocStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        model_id = self.node.try_get_context("bedrockModelId") or _DEFAULT_BEDROCK_MODEL_ID

        bucket = self._create_bucket()
        principal = self._create_pipeline_user(bucket, model_id)

        CfnOutput(self, "BucketName", value=bucket.bucket_name, description="Unit B の S3_BUCKET に転記")
        CfnOutput(self, "PipelineUserName", value=principal.user_name, description="ローカル CLI 用 IAM ユーザ")
        CfnOutput(self, "BedrockModelId", value=model_id, description="Unit B の BEDROCK_MODEL_ID に転記")

    def _create_bucket(self) -> s3.Bucket:
        """処理用バケット（NFR-SEC-01/03・BR-IO-01/04）。"""
        return s3.Bucket(
            self,
            "ProcessingBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,  # SSE-S3（Q2=A, NFR-SEC-01）
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,  # 公開遮断（NFR-SEC-03）
            enforce_ssl=True,  # TLS 強制（aws:SecureTransport=false を拒否, NFR-SEC-01）
            versioned=False,  # 機微データ滞留回避（PoC簡素化）
            lifecycle_rules=[
                s3.LifecycleRule(
                    # アプリは成功後に即削除（BR-IO-02）。残骸はライフサイクルで確実に消す（保険）。
                    expiration=Duration.days(_LIFECYCLE_EXPIRATION_DAYS),
                    abort_incomplete_multipart_upload_after=Duration.days(1),
                )
            ],
            # PoC: destroy で確実に撤去（中身も削除）。本番運用では RETAIN を検討。
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

    def _create_pipeline_user(self, bucket: s3.Bucket, model_id: str) -> iam.User:
        """ローカル CLI 用の最小権限 IAM 主体（NFR-SEC-02）。"""
        user = iam.User(self, "PipelineUser")

        # S3: 対象バケットに限定（ワイルドカード回避）。
        user.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
                resources=[bucket.arn_for_objects("*")],
            )
        )
        # バケットレベル読み取り: ListBucket に加え、アプリ起動時の前提確認
        # （S3Io.verify_preconditions → get_bucket_encryption / get_public_access_block,
        # NFR-SEC-01/03・BR-IO-01）が要求する暗号化・公開ブロックの読み取りを許可する。
        # これらが無いと最小権限ユーザーでは事前確認が AccessDenied で安全停止する。
        user.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "s3:ListBucket",
                    "s3:GetEncryptionConfiguration",
                    "s3:GetBucketPublicAccessBlock",
                ],
                resources=[bucket.bucket_arn],
            )
        )

        # Transcribe: ARN 制約が弱いため action 限定（docs/design/07-インフラ設計.md の IAM 節）。
        user.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "transcribe:StartTranscriptionJob",
                    "transcribe:GetTranscriptionJob",
                    "transcribe:ListTranscriptionJobs",
                ],
                resources=["*"],
            )
        )

        # カスタム語彙（C1・FR-C1-04/05）の冪等投入に要る 3 アクション。tools/vocab の
        # 投入ツールが Create/Update/Get を呼ぶため、これが無いと AccessDenied で止まる。
        # vocabulary リソースへの ARN 絞り込みが可能か一次情報で確認できていないため、
        # 上のジョブ系と同じく action 限定 + `*` に倒す（確信度：中）。
        user.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "transcribe:CreateVocabulary",
                    "transcribe:UpdateVocabulary",
                    "transcribe:GetVocabulary",
                ],
                resources=["*"],
            )
        )

        # Bedrock: 推論プロファイル ARN ＋ 束ねられた基盤モデル ARN（各リージョン）に限定
        # （NFR-SEC-02）。新しめのモデルはオンデマンド不可・プロファイル経由のみのため、
        # プロファイルだけでなく基盤モデル ARN にも許可が要る（双方なしでは AccessDenied）。
        bedrock_resources = [
            f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/{model_id}",
            *(
                f"arn:aws:bedrock:{region}::foundation-model/{_BEDROCK_FOUNDATION_MODEL}"
                for region in _BEDROCK_PROFILE_REGIONS
            ),
        ]
        user.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["bedrock:InvokeModel"],
                resources=bedrock_resources,
            )
        )
        return user
