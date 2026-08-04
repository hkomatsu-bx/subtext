"""資格情報の疎通確認（auth_check）と CLI `--check-auth` の単体テスト。

実 AWS は使わない。boto3 モジュールを差し替え（`boto3_module` seam）、STS 応答の代わりに
例外を投げさせて分類・リトライ・出力書式を固定する。

身元情報を出さないこと（BR-H2-AUTH-04）は「GetCallerIdentity の応答を触っていない」ことを、
返り値にアカウント ID を混ぜたフェイクで確認する。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from subtext_postmeeting import auth_check, auth_policy, aws, cli, mp4_to_vtt
from subtext_postmeeting.auth_policy import AuthErrorKind
from subtext_postmeeting.config import PipelineConfig
from subtext_postmeeting.errors import PipelineError

_ACCOUNT_ID = "123456789012"
_ARN = "arn:aws:sts::123456789012:assumed-role/subtext/operator"


class _FakeSession:
    def __init__(self, profile: str | None) -> None:
        self.profile_name = profile


class _FakeSts:
    """STS クライアントのフェイク。`errors` を順に送出し、尽きたら成功応答を返す。"""

    def __init__(self, errors: list[Exception] | None = None) -> None:
        self._errors = list(errors or [])
        self.calls = 0

    def get_caller_identity(self) -> dict[str, str]:
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        # 実 API と同じく身元情報を返す（呼び出し側が触っていないことを確かめるため）。
        return {"Account": _ACCOUNT_ID, "Arn": _ARN, "UserId": "AIDAEXAMPLE"}


class _FakeBoto3:
    def __init__(
        self,
        *,
        sts: _FakeSts | None = None,
        profile: str | None = "default",
        client_error: Exception | None = None,
    ) -> None:
        self._sts = sts if sts is not None else _FakeSts()
        self._profile = profile
        self._client_error = client_error

    def client(self, service: str, region_name: str | None = None) -> Any:
        if self._client_error is not None:
            raise self._client_error
        assert service == "sts"
        return self._sts

    def Session(self) -> _FakeSession:  # noqa: N802 boto3 の API 名に合わせる
        return _FakeSession(self._profile)


def _client_error(code: str) -> Exception:
    """botocore ClientError 相当（`response.Error.Code` で分類される）を作る。"""
    exc = Exception("boom")
    exc.response = {"Error": {"Code": code}}  # type: ignore[attr-defined]
    return exc


class _UnclassifiedClientError(Exception):
    """型名もエラーコードも既知でない例外（安全側＝UNKNOWN に倒す対象）。"""


class _ProfileNotFound(Exception):
    """指定プロファイル不在（botocore の同名例外を模す）。"""


class _NoCredentialsError(Exception):
    """型名で EXPIRED に分類される系（botocore の同名例外を模す）。"""


# 分類は型名で行うため、botocore と同じ名前に揃える（テスト間で共有するので実行順に依存させない）。
_ProfileNotFound.__name__ = "ProfileNotFound"
_NoCredentialsError.__name__ = "NoCredentialsError"


class TestProbeCredentials:
    def test_valid_credentials_report_ok(self) -> None:
        probe = auth_check.probe_credentials("ap-northeast-1", boto3_module=_FakeBoto3())

        assert probe.ok
        assert probe.status == "ok"
        assert probe.kind is None

    def test_profile_name_is_reported(self) -> None:
        probe = auth_check.probe_credentials("ap-northeast-1", boto3_module=_FakeBoto3(profile="subtext-dev"))

        assert probe.profile == "subtext-dev"

    def test_missing_profile_falls_back_to_default(self) -> None:
        # boto3 は profile 未指定時に None を返す。表示用に既定名へ倒す。
        probe = auth_check.probe_credentials("ap-northeast-1", boto3_module=_FakeBoto3(profile=None))

        assert probe.profile == "default"

    def test_expired_token_is_classified_without_raising(self) -> None:
        sts = _FakeSts([_client_error("ExpiredToken")])
        probe = auth_check.probe_credentials("ap-northeast-1", boto3_module=_FakeBoto3(sts=sts))

        assert probe.kind is AuthErrorKind.EXPIRED
        assert probe.status == "expired"

    def test_access_denied_is_forbidden(self) -> None:
        sts = _FakeSts([_client_error("AccessDenied")])
        probe = auth_check.probe_credentials("ap-northeast-1", boto3_module=_FakeBoto3(sts=sts))

        assert probe.kind is AuthErrorKind.FORBIDDEN

    def test_client_build_failure_is_classified(self) -> None:
        # client 生成時の例外も分類して返す（get_caller_identity まで到達しない経路）。
        error = _UnclassifiedClientError("something went wrong")
        probe = auth_check.probe_credentials("ap-northeast-1", boto3_module=_FakeBoto3(client_error=error))

        # 型名もエラーコードも既知でなければ UNKNOWN（安全側＝恒久扱い）。
        assert probe.kind is AuthErrorKind.UNKNOWN

    def test_no_credentials_error_is_expired(self) -> None:
        error = _NoCredentialsError("Unable to locate credentials")
        probe = auth_check.probe_credentials("ap-northeast-1", boto3_module=_FakeBoto3(client_error=error))

        assert probe.kind is AuthErrorKind.EXPIRED

    def test_transient_error_is_retried_then_succeeds(self) -> None:
        sts = _FakeSts([_client_error("Throttling")])
        slept: list[float] = []

        probe = auth_check.probe_credentials(
            "ap-northeast-1", boto3_module=_FakeBoto3(sts=sts), sleep=slept.append
        )

        assert probe.ok
        assert sts.calls == 2  # 1 回目は一過性障害、2 回目で成功
        assert len(slept) == 1  # バックオフを挟んでいる

    def test_transient_error_gives_up_after_limit(self) -> None:
        errors = [_client_error("Throttling") for _ in range(5)]
        sts = _FakeSts(errors)

        probe = auth_check.probe_credentials(
            "ap-northeast-1", boto3_module=_FakeBoto3(sts=sts), sleep=lambda _s: None
        )

        assert probe.kind is AuthErrorKind.TRANSIENT
        assert sts.calls == 3  # 初回 + DEFAULT_MAX_RETRIES(2)


class TestCheckAuthCli:
    def _run(self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, probe: auth_check.AuthProbe):
        monkeypatch.setattr(auth_check, "probe_credentials", lambda *a, **k: probe)
        monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
        monkeypatch.delenv("S3_BUCKET", raising=False)
        code = cli.main(["--check-auth"])
        return code, capsys.readouterr().out

    def test_ok_prints_machine_readable_lines(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code, out = self._run(capsys, monkeypatch, auth_check.AuthProbe("default", None))

        assert code == 0
        assert "authProfile=default" in out
        assert "authRegion=ap-northeast-1" in out
        assert "authStatus=ok" in out

    def test_failure_exits_nonzero_with_remediation(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code, out = self._run(capsys, monkeypatch, auth_check.AuthProbe("default", AuthErrorKind.EXPIRED))

        assert code == 1
        assert "authStatus=expired" in out
        assert "aws login" in out  # 対処メッセージ（auth_policy の正本）

    def test_output_never_contains_identity(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """アカウント ID・ARN を出さない（BR-H2-AUTH-04）。

        probe 自体は本物を通し、STS が身元情報を返しても出力に現れないことを確かめる
        （差し替えるのは boto3 だけ。ここを fake probe にすると検証が空振りする）。
        """
        real_probe = auth_check.probe_credentials
        monkeypatch.setattr(
            auth_check,
            "probe_credentials",
            lambda region, **k: real_probe(region, boto3_module=_FakeBoto3()),
        )
        monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
        monkeypatch.delenv("S3_BUCKET", raising=False)

        code = cli.main(["--check-auth"])
        out = capsys.readouterr().out

        assert code == 0
        assert _ACCOUNT_ID not in out
        assert _ARN not in out

    def test_check_auth_does_not_require_mode(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # --check-auth は --mode を取らない（argparse の required を外した経路の回帰防止）。
        code, out = self._run(capsys, monkeypatch, auth_check.AuthProbe("default", None))

        assert code == 0
        assert "authStatus=ok" in out

    def test_mode_is_still_required_for_pipeline(self) -> None:
        # --check-auth なしで --mode を省くと、argparse ではなく自前の PipelineError で 1 を返す。
        assert cli.main([]) == 1


class TestProfileSelection:
    def test_resolve_profile_prefers_cli(self) -> None:
        assert aws.resolve_profile("from-cli", "from-env") == "from-cli"

    def test_resolve_profile_falls_back_to_env_or_dotenv(self) -> None:
        # 環境変数と .env の優先関係は PipelineConfig.from_env が解決済み（env_profile に入る）。
        assert aws.resolve_profile(None, "from-env") == "from-env"

    def test_resolve_profile_unset_is_empty(self) -> None:
        assert aws.resolve_profile(None, "") == ""
        assert aws.resolve_profile("  ", "") == ""

    def test_blank_cli_profile_does_not_mask_environment(self) -> None:
        """`--profile " "` で環境変数・.env の指定を打ち消さないこと。

        打ち消すと黙って SDK 既定（別アカウント）へ倒れ、課金先が変わる。
        """
        assert aws.resolve_profile(" ", "from-env") == "from-env"

    def test_remediation_messages_are_line_wrapped(self) -> None:
        """対処メッセージは改行で区切る（ハーネスの TUI ログは折り返さない）。

        1 行に詰めると「次に何をするか」を書いた後半が画面外へ切れる。
        """
        for kind in AuthErrorKind:
            message = auth_policy.remediation_message(kind)

            assert "\n" in message, kind
            assert all(len(line) <= 70 for line in message.splitlines()), kind

    def test_apply_profile_sets_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AWS_PROFILE", raising=False)

        aws.apply_profile("subtext-dev")

        assert os.environ["AWS_PROFILE"] == "subtext-dev"

    def test_apply_profile_does_not_force_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """未指定のとき `AWS_PROFILE=default` を入れてはいけない。

        botocore はプロファイルが明示されていると `~/.aws/config` に該当セクションが無い場合
        `ProfileNotFound` を送出する（未設定なら許容する）。`[default]` を持たない環境
        （資格情報を環境変数で渡す運用・CI）で、今まで動いていたものが壊れる。
        """
        monkeypatch.delenv("AWS_PROFILE", raising=False)

        aws.apply_profile("")

        assert "AWS_PROFILE" not in os.environ

    def test_dotenv_profile_reaches_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # 以前は .env の AWS_PROFILE を読んでも捨てていた（フィールドが無かった）。
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        monkeypatch.delenv("S3_BUCKET", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("AWS_PROFILE=from-dotenv\nAWS_REGION=ap-northeast-1\n", encoding="utf-8")

        config = PipelineConfig.from_env(env_file, require_s3=False)

        assert config.aws_profile == "from-dotenv"

    def test_environment_wins_over_dotenv(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_PROFILE", "from-env")
        env_file = tmp_path / ".env"
        env_file.write_text("AWS_PROFILE=from-dotenv\n", encoding="utf-8")

        config = PipelineConfig.from_env(env_file, require_s3=False)

        assert config.aws_profile == "from-env"

    def test_check_auth_reports_requested_profile(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """存在しないプロファイル名でも「default で失敗した」と報告しないこと。

        `boto3.Session()` は存在しないプロファイルだと `ProfileNotFound` を投げるため、
        Session から名前を取ると既定値へ落ちてしまう（実際に踏んだ挙動の回帰防止）。
        """
        monkeypatch.setattr(
            auth_check,
            "probe_credentials",
            lambda region, *, profile="", **k: auth_check.AuthProbe(profile, AuthErrorKind.PROFILE_NOT_FOUND),
        )
        monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
        monkeypatch.delenv("S3_BUCKET", raising=False)

        code = cli.main(["--check-auth", "--profile", "typo-profile"])
        out = capsys.readouterr().out

        assert code == 1
        assert "authProfile=typo-profile" in out
        assert "authStatus=profile_not_found" in out
        assert "aws login" not in out  # 再ログインでは直らないので誘導しない
        assert "AWS_PROFILE" in out  # 綴りの確認を促す

    def test_mp4_to_vtt_applies_profile_before_converting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """mp4→VTT も課金経路（Transcribe）なので、変換前にプロファイルを適用すること。"""
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        monkeypatch.setenv("S3_BUCKET", "bucket")  # require_s3=True の経路
        monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
        mp4 = tmp_path / "recording.mp4"
        mp4.write_bytes(b"\x00")
        seen: list[str | None] = []

        def fake_convert(*args: Any, **kwargs: Any) -> Path:
            seen.append(os.environ.get("AWS_PROFILE"))  # 変換の時点で解決済みであること
            return tmp_path / "recording.vtt"

        monkeypatch.setattr(mp4_to_vtt, "convert", fake_convert)

        code = mp4_to_vtt.main([str(mp4), "--profile", "subtext-dev"])

        assert code == 0
        assert seen == ["subtext-dev"]

    def test_profile_not_found_is_classified(self) -> None:
        error = _ProfileNotFound("The config profile (x) could not be found")

        probe = auth_check.probe_credentials(
            "ap-northeast-1", profile="x", boto3_module=_FakeBoto3(client_error=error)
        )

        assert probe.kind is AuthErrorKind.PROFILE_NOT_FOUND
        assert probe.profile == "x"


class TestCredentialCheckerStillFailsFast:
    def test_checker_raises_actionable_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """段 0 の事前チェックは従来どおり課金前に PipelineError で止まること。"""
        from subtext_postmeeting import pipeline as pl

        monkeypatch.setattr(
            auth_check,
            "probe_credentials",
            lambda *a, **k: auth_check.AuthProbe("default", AuthErrorKind.EXPIRED),
        )
        with pytest.raises(PipelineError) as exc:
            pl.make_credential_checker("ap-northeast-1")()

        assert exc.value.failed_stage == "auth"
        assert "aws login" in str(exc.value)
