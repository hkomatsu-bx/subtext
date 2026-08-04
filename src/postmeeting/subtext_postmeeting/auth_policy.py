"""AWS 認証エラーの分類・リトライ判定・バックオフ計算（FR-H2-07 / DG-H2-1）。

純粋関数のみ。boto3/botocore を import せず、例外型名・エラーコード文字列から分類する
（NFR-H2-03: AWS 非依存で単体テスト可能）。fail-fast（BR-ERR-03）は維持し、リトライは
一過性障害のみ・有限回に限定する（BR-H2-AUTH-03）。恒久障害（期限切れ/権限不足/不明）は
課金段の前に actionable な停止へ倒す（BR-H2-AUTH-02）。
"""

from __future__ import annotations

import random
from enum import Enum
from typing import Callable


class AuthErrorKind(str, Enum):
    """認証/認可失敗の分類。"""

    TRANSIENT = "transient"  # スロットリング・一時障害 → 有限回リトライ
    EXPIRED = "expired"  # 資格情報/SSO トークン期限切れ → 要対話再ログイン
    FORBIDDEN = "forbidden"  # 権限不足(403) → 再ログインでは解決しない
    PROFILE_NOT_FOUND = "profile_not_found"  # 指定プロファイルが ~/.aws/config に無い → 綴り/設定の問題
    UNKNOWN = "unknown"  # 分類不能 → 安全側（課金前停止）で恒久扱い


# botocore ClientError の Error.Code による分類。
_TRANSIENT_CODES = frozenset(
    {
        "Throttling",
        "ThrottlingException",
        "TooManyRequestsException",
        "RequestLimitExceeded",
        "ProvisionedThroughputExceededException",
        "RequestTimeout",
        "RequestTimeoutException",
        "ServiceUnavailable",
        "ServiceUnavailableException",
        "InternalServerError",
        "InternalFailure",
        "SlowDown",
    }
)
_EXPIRED_CODES = frozenset(
    {
        "ExpiredToken",
        "ExpiredTokenException",
        "RequestExpired",
        "InvalidClientTokenId",
        "TokenRefreshRequired",
        "InvalidToken",
    }
)
_FORBIDDEN_CODES = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "UnauthorizedOperation",
        "AuthorizationError",
        "UnrecognizedClientException",
    }
)
# ClientError 以外（資格情報未解決・SSO トークン失効など）を型名で判定。
_EXPIRED_EXC_TYPES = frozenset(
    {
        "NoCredentialsError",
        "CredentialRetrievalError",
        "TokenRetrievalError",
        "UnauthorizedSSOTokenError",
        "SSOTokenLoadError",
        "RefreshWithMFAUnsupportedError",
    }
)

# 指定プロファイル自体が見つからない（`--profile` の綴り違い・`.env` の設定漏れ）。
# 再ログインでは解決しないため期限切れと分けて扱う。
_PROFILE_EXC_TYPES = frozenset({"ProfileNotFound"})

# 認証チェック/課金段でのリトライ上限（一過性障害のみ対象）。
DEFAULT_MAX_RETRIES = 2


def classify_code(code: str) -> AuthErrorKind:
    """エラーコード文字列を分類する（純粋）。未知は UNKNOWN。"""
    if code in _TRANSIENT_CODES:
        return AuthErrorKind.TRANSIENT
    if code in _EXPIRED_CODES:
        return AuthErrorKind.EXPIRED
    if code in _FORBIDDEN_CODES:
        return AuthErrorKind.FORBIDDEN
    return AuthErrorKind.UNKNOWN


def classify_exception(exc: BaseException) -> AuthErrorKind:
    """boto3/botocore 例外を分類する（duck-typing・boto3 非 import）。

    型名で資格情報未解決系を先に判定し、次に ClientError の Error.Code を見る。
    """
    if type(exc).__name__ in _PROFILE_EXC_TYPES:
        return AuthErrorKind.PROFILE_NOT_FOUND
    if type(exc).__name__ in _EXPIRED_EXC_TYPES:
        return AuthErrorKind.EXPIRED
    code = _extract_error_code(exc)
    if code:
        return classify_code(code)
    return AuthErrorKind.UNKNOWN


def _extract_error_code(exc: BaseException) -> str:
    """botocore ClientError の Error.Code を安全に取り出す（無ければ空文字）。"""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error", {})
        if isinstance(error, dict):
            return str(error.get("Code", ""))
    return ""


def should_retry(kind: AuthErrorKind, attempt: int, max_attempts: int) -> bool:
    """一過性障害かつ残り試行があるときだけリトライする（BR-H2-AUTH-03）。

    attempt は 0 始まりの実施済み回数。
    """
    return kind == AuthErrorKind.TRANSIENT and attempt < max_attempts


def next_backoff(
    attempt: int,
    *,
    base_sec: float = 0.5,
    factor: float = 2.0,
    max_sec: float = 8.0,
    rand: Callable[[], float] = random.random,
) -> float:
    """フルジッタ付き指数バックオフ秒を返す（純粋・rand は seam）。

    attempt は 0 始まり。上限 max_sec を超えないようキャップしたうえで [0, cap) に散らす。
    """
    if attempt < 0:
        attempt = 0
    capped = min(base_sec * (factor**attempt), max_sec)
    return capped * rand()


def remediation_message(kind: AuthErrorKind) -> str:
    """ユーザーが次に取るべき行動を示す actionable メッセージ（BR-ERR-04: 機微は載せない）。

    **改行で区切る。** 呼び出し側にはハーネスの TUI ログ（折り返さない `Log` ウィジェット。
    行ごとに書き出す）があり、1 行が長いと「次に何をするか」を書いた後半が画面の外へ切れる。
    """
    if kind == AuthErrorKind.PROFILE_NOT_FOUND:
        return (
            "指定された AWS プロファイルが見つかりません（~/.aws/config を確認）。\n"
            "  `--profile`・環境変数 AWS_PROFILE・.env の AWS_PROFILE の綴りを確認してください。\n"
            "  再ログインでは解決しません。"
        )
    if kind == AuthErrorKind.FORBIDDEN:
        return (
            "AWS 権限が不足しています（AccessDenied）。\n"
            "  IAM ポリシーまたは Bedrock のモデルアクセスを確認してください。\n"
            "  再ログインでは解決しません。"
        )
    return (
        "AWS 認証が無効または期限切れです。\n"
        "  `! aws login`（または `aws sso login`）で再認証し、同じコマンドを再実行してください。\n"
        "  中間成果物は再利用され再課金されません。"
    )
