"""STS による資格情報の疎通確認（FR-H2-07 / FR-H2-11・BR-H2-AUTH-02/04）。

課金は発生しない（`GetCallerIdentity` のみ）。分類・リトライ・対処メッセージは `auth_policy`
（純粋・boto3 非依存）に委ね、ここは boto3 の呼び出しと結果の畳み込みだけを持つ。

呼び出し口は 2 つある。段 0 の事前チェック（`pipeline.make_credential_checker`）と、
CLI の `--check-auth`（会議ハーネスが起動時に叩く）である。どちらも本モジュールの
`probe_credentials` を通すことで、確認経路が増えても判定と文言の正本を 1 つに保つ。

**応答から身元情報を取り出さない**（BR-H2-AUTH-04）。`GetCallerIdentity` はアカウント ID・
ARN・ユーザー ID を返すが、呼び出し側はこれを画面やログへ流すため、成否だけを使う
（BR-ERR-04・NFR-SEC-04）。profile 名は環境設定であり秘密ではないため返す。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from . import auth_policy, aws
from .auth_policy import AuthErrorKind

# 認証確認の段名（PipelineError の failed_stage に載る）。
STAGE = "auth"
# profile を解決できなかったときの表示値（boto3 の既定と同じ語を使う）。
_DEFAULT_PROFILE = "default"


@dataclass(frozen=True)
class AuthProbe:
    """疎通確認の結果。`kind` が None なら資格情報は有効。"""

    profile: str
    kind: AuthErrorKind | None

    @property
    def ok(self) -> bool:
        return self.kind is None

    @property
    def status(self) -> str:
        """機械可読な状態文字列（`ok` または分類名）。"""
        return "ok" if self.kind is None else self.kind.value


def probe_credentials(
    region: str,
    *,
    profile: str = "",
    boto3_module: Any | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> AuthProbe:
    """STS で資格情報の有効性を確認する（課金なし）。

    失敗は送出せず分類して返す（呼び出し側が停止させるか表示に留めるかを決められるように）。
    boto3 未導入だけは例外（`PipelineError`）のまま通す。分類の失敗ではなく環境の不備であり、
    `uv sync` という別の対処が必要なためである。

    `profile` は解決済みのプロファイル名（空なら SDK の解決結果を報告に使う）。**呼び出し側から
    渡すのは、存在しないプロファイル名だと `boto3.Session()` 自体が `ProfileNotFound` を投げて
    名前を取れず、「default で失敗した」という誤った報告になるためである。**

    `boto3_module` / `sleep` はテスト用の seam。
    """
    boto3 = boto3_module if boto3_module is not None else aws.import_boto3(STAGE)
    reported_profile = profile or _session_profile(boto3)
    try:
        # client 生成時にも資格情報解決エラー（ProfileNotFound / NoCredentialsError 等）が出る。
        # ここで捕らえないと生トレースバックが呼び出し側へ抜ける。
        client = boto3.client("sts", region_name=region)
    except Exception as exc:  # noqa: BLE001 分類して返す（握り潰さない）
        return AuthProbe(reported_profile, auth_policy.classify_exception(exc))

    attempt = 0
    while True:
        try:
            client.get_caller_identity()  # 応答は使わない（BR-H2-AUTH-04）
            return AuthProbe(reported_profile, None)
        except Exception as exc:  # noqa: BLE001 分類して返す（握り潰さない）
            kind = auth_policy.classify_exception(exc)
            if auth_policy.should_retry(kind, attempt, auth_policy.DEFAULT_MAX_RETRIES):
                sleep(auth_policy.next_backoff(attempt))
                attempt += 1
                continue
            return AuthProbe(reported_profile, kind)


def _session_profile(boto3: Any) -> str:
    """SDK が解決した profile 名（未設定なら `default`、判定できなければ空）。

    プロファイルが明示指定されていないときの表示用。`Session()` の生成は資格情報を解決しないが、
    存在しないプロファイルが環境変数で指定されていると `ProfileNotFound` を投げるため、その場合は
    空を返す（`default` を返すと「default で失敗した」という誤った報告になる）。
    """
    try:
        return str(boto3.Session().profile_name or _DEFAULT_PROFILE)
    except Exception:  # noqa: BLE001 表示のための情報であり、取れなくても確認自体は続ける
        return ""
