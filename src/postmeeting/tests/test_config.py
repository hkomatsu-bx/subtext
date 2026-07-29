"""設定束縛のテスト（FR-15）。必須欠落の fail-fast・型変換を確認する。"""

from __future__ import annotations

from pathlib import Path

import pytest

from subtext_postmeeting.config import PipelineConfig
from subtext_postmeeting.errors import PipelineError


def _write_env(tmp_path: Path, lines: list[str]) -> Path:
    env = tmp_path / ".env"
    env.write_text("\n".join(lines), encoding="utf-8")
    return env


class TestRequiredValues:
    def test_missing_s3_bucket_fails_fast(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("S3_BUCKET", raising=False)
        env = _write_env(tmp_path, ["AWS_REGION=ap-northeast-1"])
        with pytest.raises(PipelineError):
            PipelineConfig.from_env(env)

    def test_loads_with_s3_bucket(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("S3_BUCKET", raising=False)
        env = _write_env(tmp_path, ["S3_BUCKET=my-bucket"])
        config = PipelineConfig.from_env(env)
        assert config.s3_bucket == "my-bucket"
        assert config.aws_region == "ap-northeast-1"  # 既定
        assert config.max_speakers == 5

    def test_require_bedrock_model_raises_when_empty(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("S3_BUCKET", raising=False)
        monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
        env = _write_env(tmp_path, ["S3_BUCKET=my-bucket"])
        config = PipelineConfig.from_env(env)
        with pytest.raises(PipelineError):
            config.require_bedrock_model()


class TestParsing:
    def test_parses_overrides(self, tmp_path: Path, monkeypatch) -> None:
        for key in ("S3_BUCKET", "MAX_SPEAKERS", "KEEP_S3", "S3_PREFIX"):
            monkeypatch.delenv(key, raising=False)
        env = _write_env(
            tmp_path,
            [
                "S3_BUCKET=b",
                "MAX_SPEAKERS=3",
                "KEEP_S3=true",
                "S3_PREFIX=/custom/path/",
                "BEDROCK_MODEL_ID=anthropic.claude",
            ],
        )
        config = PipelineConfig.from_env(env)
        assert config.max_speakers == 3
        assert config.keep_s3 is True
        assert config.s3_prefix == "custom/path/"  # 正規化（前後スラッシュ）
        assert config.require_bedrock_model() == "anthropic.claude"

    def test_invalid_int_fails_fast(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("S3_BUCKET", raising=False)
        monkeypatch.delenv("MAX_SPEAKERS", raising=False)
        env = _write_env(tmp_path, ["S3_BUCKET=b", "MAX_SPEAKERS=abc"])
        with pytest.raises(PipelineError):
            PipelineConfig.from_env(env)

    def test_env_does_not_override_real_environment(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("S3_BUCKET", "from-environment")
        env = _write_env(tmp_path, ["S3_BUCKET=from-file"])
        config = PipelineConfig.from_env(env)
        assert config.s3_bucket == "from-environment"  # 環境変数優先

    def test_blank_value_falls_back_to_default(self, tmp_path: Path, monkeypatch) -> None:
        # 空文字の AWS_REGION は「未設定」として既定へ倒す（boto3 生成時の不可解なエラー回避）。
        for key in ("S3_BUCKET", "AWS_REGION"):
            monkeypatch.delenv(key, raising=False)
        env = _write_env(tmp_path, ["S3_BUCKET=b", "AWS_REGION="])
        config = PipelineConfig.from_env(env)
        assert config.aws_region == "ap-northeast-1"  # 既定へフォールバック


class TestVocabularyName:
    """C1・FR-C1-02: カスタム語彙名は任意。未設定なら空（語彙なし）。"""

    def test_vocabulary_name_bound_when_set(self, tmp_path: Path, monkeypatch) -> None:
        for key in ("S3_BUCKET", "VOCABULARY_NAME"):
            monkeypatch.delenv(key, raising=False)
        env = _write_env(tmp_path, ["S3_BUCKET=b", "VOCABULARY_NAME=subtext-ja"])
        config = PipelineConfig.from_env(env)
        assert config.vocabulary_name == "subtext-ja"

    def test_vocabulary_name_empty_when_unset(self, tmp_path: Path, monkeypatch) -> None:
        for key in ("S3_BUCKET", "VOCABULARY_NAME"):
            monkeypatch.delenv(key, raising=False)
        env = _write_env(tmp_path, ["S3_BUCKET=b"])
        config = PipelineConfig.from_env(env)
        assert config.vocabulary_name == ""
