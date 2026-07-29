"""CDK エントリ（Q8=B）。

Subtext PoC スタックを ap-northeast-1（NFR-07）に定義する。環境（アカウント/リージョン）は
デフォルト認証情報の解決に委ねる（NFR-SEC-07）。Bedrock のモデル有効化は CDK 対象外（手動・
deployment-architecture.md の P0 チェックリスト参照）。
"""

import os

import aws_cdk as cdk

from stacks.subtext_poc_stack import SubtextPocStack

app = cdk.App()

SubtextPocStack(
    app,
    "SubtextPocStack",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "ap-northeast-1"),
    ),
)

# 共通タグ（棚卸し・コスト配分）。Tags.of(app) はスタックおよびタグ可能な
# 配下リソース（S3 / IAM User / IAM Role / Lambda 等）へ伝播する。
cdk.Tags.of(app).add("Project", "Subtext")
cdk.Tags.of(app).add("Component", "unit-b-postmeeting")
cdk.Tags.of(app).add("Environment", "PoC")
cdk.Tags.of(app).add("ManagedBy", "CDK")
cdk.Tags.of(app).add("Owner", "subtext-poc")

app.synth()
