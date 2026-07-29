"""認証ポリシー純粋関数のテスト（FR-H2-07 / NFR-H2-03・AWS 非依存）。"""

from __future__ import annotations

import pytest

from subtext_postmeeting import auth_policy
from subtext_postmeeting.auth_policy import AuthErrorKind


class _FakeClientError(Exception):
    """botocore ClientError の最小モック（response.Error.Code を持つ）。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class NoCredentialsError(Exception):
    """型名で分類されることを確認するためのダミー（botocore 同名型を模す）。"""


class TestClassifyCode:
    @pytest.mark.parametrize(
        "code,expected",
        [
            ("ThrottlingException", AuthErrorKind.TRANSIENT),
            ("SlowDown", AuthErrorKind.TRANSIENT),
            ("ExpiredToken", AuthErrorKind.EXPIRED),
            ("InvalidClientTokenId", AuthErrorKind.EXPIRED),
            ("AccessDeniedException", AuthErrorKind.FORBIDDEN),
            ("UnrecognizedClientException", AuthErrorKind.FORBIDDEN),
            ("SomethingWeird", AuthErrorKind.UNKNOWN),
            ("", AuthErrorKind.UNKNOWN),
        ],
    )
    def test_classify_code(self, code: str, expected: AuthErrorKind) -> None:
        assert auth_policy.classify_code(code) == expected


class TestClassifyException:
    def test_client_error_code_is_classified(self) -> None:
        assert auth_policy.classify_exception(_FakeClientError("ThrottlingException")) == (AuthErrorKind.TRANSIENT)
        assert auth_policy.classify_exception(_FakeClientError("ExpiredToken")) == (AuthErrorKind.EXPIRED)

    def test_credential_exc_type_is_expired(self) -> None:
        assert auth_policy.classify_exception(NoCredentialsError()) == AuthErrorKind.EXPIRED

    def test_unmappable_exception_is_unknown(self) -> None:
        assert auth_policy.classify_exception(ValueError("boom")) == AuthErrorKind.UNKNOWN


class TestShouldRetry:
    def test_retry_only_transient_within_budget(self) -> None:
        assert auth_policy.should_retry(AuthErrorKind.TRANSIENT, attempt=0, max_attempts=2)
        assert auth_policy.should_retry(AuthErrorKind.TRANSIENT, attempt=1, max_attempts=2)

    def test_no_retry_when_budget_exhausted(self) -> None:
        assert not auth_policy.should_retry(AuthErrorKind.TRANSIENT, attempt=2, max_attempts=2)

    def test_no_retry_for_permanent_kinds(self) -> None:
        for kind in (AuthErrorKind.EXPIRED, AuthErrorKind.FORBIDDEN, AuthErrorKind.UNKNOWN):
            assert not auth_policy.should_retry(kind, attempt=0, max_attempts=2)


class TestNextBackoff:
    def test_full_jitter_within_capped_window(self) -> None:
        # rand=1.0 で上限値、rand=0.0 で 0。attempt が増えると窓が広がるが max_sec でキャップ。
        assert auth_policy.next_backoff(0, base_sec=0.5, factor=2.0, max_sec=8.0, rand=lambda: 1.0) == 0.5
        assert auth_policy.next_backoff(2, base_sec=0.5, factor=2.0, max_sec=8.0, rand=lambda: 1.0) == 2.0
        assert auth_policy.next_backoff(10, base_sec=0.5, factor=2.0, max_sec=8.0, rand=lambda: 1.0) == 8.0
        assert auth_policy.next_backoff(3, base_sec=0.5, factor=2.0, max_sec=8.0, rand=lambda: 0.0) == 0.0

    def test_negative_attempt_is_clamped(self) -> None:
        assert auth_policy.next_backoff(-5, base_sec=1.0, factor=2.0, max_sec=8.0, rand=lambda: 1.0) == 1.0


class TestRemediationMessage:
    def test_forbidden_message_mentions_permissions(self) -> None:
        msg = auth_policy.remediation_message(AuthErrorKind.FORBIDDEN)
        assert "権限" in msg and "再ログイン" in msg

    def test_expired_message_mentions_relogin(self) -> None:
        msg = auth_policy.remediation_message(AuthErrorKind.EXPIRED)
        assert "login" in msg and "再実行" in msg
