"""manage_vocabulary の単体テスト（C1・NFR-C1-04）。

純粋部（validate_table）と upsert 分岐（fake transcribe client）を検証する。boto3 は不要
（_build_clients/main の AWS 経路は呼ばない）。実行: uv run --with pytest pytest tools/vocab/
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import manage_vocabulary as mv  # noqa: E402


def _table(*rows: str) -> str:
    header = "\t".join(("Phrase", "SoundsLike", "IPA", "DisplayAs"))
    return "\n".join((header, *rows)) + "\n"


# --- validate_table（純粋） ---------------------------------------------------
def test_validate_table_accepts_valid_table() -> None:
    text = _table("BeeX\t\t\tBeeX", "Subtext\t\t\tSubtext")
    result = mv.validate_table(text)
    assert result.ok
    assert result.phrase_count == 2


def test_validate_table_rejects_wrong_header() -> None:
    text = "Term\tDisplay\nBeeX\tBeeX\n"
    result = mv.validate_table(text)
    assert not result.ok
    assert any("4 列ヘッダ" in e for e in result.errors)


def test_validate_table_rejects_empty_phrase() -> None:
    text = _table("\t\t\tBeeX")
    result = mv.validate_table(text)
    assert not result.ok
    assert any("Phrase が空" in e for e in result.errors)


def test_validate_table_rejects_space_in_phrase() -> None:
    text = _table("Los Angeles\t\t\tLos Angeles")
    result = mv.validate_table(text)
    assert not result.ok
    assert any("空白" in e for e in result.errors)


def test_validate_table_rejects_empty_file() -> None:
    result = mv.validate_table("\n")
    assert not result.ok


def test_validate_table_rejects_header_only() -> None:
    result = mv.validate_table(_table())
    assert not result.ok
    assert any("1 件もありません" in e for e in result.errors)


# --- upsert_vocabulary（fake client） ----------------------------------------
class _FakeTranscribe:
    """get/create/update を記録する fake。get の戻り状態列をキューで制御する。"""

    def __init__(self, *, exists: bool, states: list[str]) -> None:
        self._exists = exists
        self._states = list(states)
        self.created = 0
        self.updated = 0
        self._first_get = True

    def get_vocabulary(self, VocabularyName: str) -> dict[str, str]:  # noqa: N803 (AWS API 名)
        # 存在判定の最初の get は exists を反映するだけで poll キューは消費しない。
        if self._first_get:
            self._first_get = False
            if not self._exists:
                raise _NotFound()
            return {"VocabularyState": "PENDING"}
        state = self._states.pop(0) if self._states else "READY"
        resp = {"VocabularyState": state}
        if state == "FAILED":
            resp["FailureReason"] = "Invalid character at line 3"
        return resp

    def create_vocabulary(self, **_: object) -> None:
        self.created += 1

    def update_vocabulary(self, **_: object) -> None:
        self.updated += 1


class _NotFound(Exception):
    def __init__(self) -> None:
        super().__init__("The requested vocabulary was not found")
        self.response = {"Error": {"Code": "NotFoundException"}}


def test_upsert_creates_when_absent() -> None:
    client = _FakeTranscribe(exists=False, states=["PENDING", "READY"])
    state = mv.upsert_vocabulary(
        client,
        name="subtext-ja",
        language="ja-JP",
        file_uri="s3://b/k.txt",
        sleeper=lambda _: None,
    )
    assert state == "READY"
    assert client.created == 1
    assert client.updated == 0


def test_upsert_updates_when_present() -> None:
    client = _FakeTranscribe(exists=True, states=["READY"])
    mv.upsert_vocabulary(
        client,
        name="subtext-ja",
        language="ja-JP",
        file_uri="s3://b/k.txt",
        sleeper=lambda _: None,
    )
    assert client.updated == 1
    assert client.created == 0


def test_upsert_raises_on_failed() -> None:
    client = _FakeTranscribe(exists=False, states=["PENDING", "FAILED"])
    with pytest.raises(RuntimeError, match="FAILED"):
        mv.upsert_vocabulary(
            client,
            name="subtext-ja",
            language="ja-JP",
            file_uri="s3://b/k.txt",
            sleeper=lambda _: None,
        )


def test_upsert_times_out() -> None:
    client = _FakeTranscribe(exists=True, states=["PENDING"] * 10)
    clock = iter([0.0, 0.0, 1000.0])  # monotonic: 開始, ループ判定で deadline 超過
    with pytest.raises(RuntimeError, match="READY になりませんでした"):
        mv.upsert_vocabulary(
            client,
            name="subtext-ja",
            language="ja-JP",
            file_uri="s3://b/k.txt",
            sleeper=lambda _: None,
            monotonic=lambda: next(clock),
            poll_timeout_sec=1,
        )
