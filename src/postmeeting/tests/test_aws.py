"""boto3 クライアント生成ヘルパのテスト。boto3 は差し替え、実 AWS は呼ばない。"""

from __future__ import annotations

from typing import Any

import pytest

from subtext_postmeeting import aws


class _FakeBoto3:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def client(self, service: str, **kwargs: Any) -> object:
        self.calls.append((service, kwargs))
        return object()


def test_bedrock_runtime_client_extends_read_timeout_and_disables_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    fake = _FakeBoto3()
    monkeypatch.setattr(aws, "import_boto3", lambda stage: fake)
    # Act
    aws.build_bedrock_runtime_client("ap-northeast-1", stage="summarize")
    # Assert
    service, kwargs = fake.calls[0]
    assert service == "bedrock-runtime"
    assert kwargs["region_name"] == "ap-northeast-1"
    assert kwargs["config"].read_timeout == 900
    assert kwargs["config"].retries == {"total_max_attempts": 1}
