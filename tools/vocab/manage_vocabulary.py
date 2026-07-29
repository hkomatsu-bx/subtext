"""Transcribe カスタム語彙の冪等な作成/更新ツール（C1・FR-C1-04/05）。

用語ファイル（table 形式 .txt）を S3 へアップロードし、`CreateVocabulary`（未存在）
または `UpdateVocabulary`（存在）で語彙を upsert する。`READY`/`FAILED` まで指数バック
オフでポーリングし、`FAILED` は FailureReason を提示して非ゼロ終了する（fail-fast）。

両ユニット（Unit C / Unit B）はこのツールが作る語彙名を各自の設定で参照するだけで、
語彙の管理には関与しない（BR-VOCAB-03）。認証情報は読み込まず AWS 既定の解決チェーン
に委譲する（BR-VOCAB-05, NFR-SEC-07）。

AWS の Custom Vocabulary table 仕様（裏取り済 2026-06-25）:
- 4 列ヘッダ必須（Phrase / SoundsLike / IPA / DisplayAs）。区切りは TAB。
- SoundsLike / IPA はサポート終了（値は無視）。Phrase 必須・DisplayAs 任意。
- ファイルは S3 に置き VocabularyFileUri で参照する（inline 不可）。

実行: uv run --with boto3 python tools/vocab/manage_vocabulary.py \
        --name subtext-ja --bucket <処理バケット>
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Windows コンソール(cp932)で日本語出力がクラッシュしないよう UTF-8 へ固定する。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_REQUIRED_HEADER = ("Phrase", "SoundsLike", "IPA", "DisplayAs")
_INITIAL_BACKOFF_SEC = 5.0
_MAX_BACKOFF_SEC = 30.0
_POLL_TIMEOUT_SEC = 300
# AWS 制約（裏取り済）: 1 ファイル 50KB / 1 エントリ 256 文字。
_MAX_FILE_BYTES = 50 * 1024
_MAX_ENTRY_CHARS = 256


@dataclass(frozen=True)
class TableValidation:
    """用語ファイル検証結果（純粋）。"""

    phrase_count: int
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_table(text: str) -> TableValidation:
    """table 形式テキストを検証する（I/O なし・単体テスト対象）。

    - 1 行目は 4 列ヘッダ（Phrase/SoundsLike/IPA/DisplayAs, TAB 区切り）。
    - 各データ行は 4 列・Phrase 非空・Phrase に空白を含まない・256 文字以内。
    - ファイルサイズ 50KB 以内。
    """
    errors: list[str] = []
    raw = text.encode("utf-8")
    if len(raw) > _MAX_FILE_BYTES:
        errors.append(f"ファイルサイズが上限 {_MAX_FILE_BYTES} バイトを超えています（{len(raw)}）。")

    lines = [ln for ln in text.splitlines() if ln.strip() != ""]
    if not lines:
        return TableValidation(0, ("用語ファイルが空です。",))

    header = tuple(lines[0].split("\t"))
    if header != _REQUIRED_HEADER:
        errors.append(
            "1 行目は TAB 区切りの 4 列ヘッダ "
            f"'{chr(9).join(_REQUIRED_HEADER)}' である必要があります（実際: {header!r}）。"
        )
        return TableValidation(0, tuple(errors))

    phrase_count = 0
    for idx, line in enumerate(lines[1:], start=2):
        cols = line.split("\t")
        if len(cols) != 4:
            errors.append(f"{idx} 行目: 列数が 4 ではありません（{len(cols)}）。空欄も TAB で区切ってください。")
            continue
        phrase = cols[0]
        if phrase.strip() == "":
            errors.append(f"{idx} 行目: Phrase が空です（必須）。")
            continue
        if any(ch.isspace() for ch in phrase):
            errors.append(f"{idx} 行目: Phrase に空白を含められません（複数語はハイフン連結）: '{phrase}'。")
        if len(phrase) > _MAX_ENTRY_CHARS:
            errors.append(f"{idx} 行目: Phrase が {_MAX_ENTRY_CHARS} 文字を超えています。")
        phrase_count += 1

    if phrase_count == 0:
        errors.append("有効な用語（データ行）が 1 件もありません。")

    return TableValidation(phrase_count, tuple(errors))


def upsert_vocabulary(
    client: Any,
    *,
    name: str,
    language: str,
    file_uri: str,
    sleeper: Any = time.sleep,
    monotonic: Any = time.monotonic,
    poll_timeout_sec: int = _POLL_TIMEOUT_SEC,
) -> str:
    """語彙を冪等に作成/更新し READY まで待つ。最終状態を返す。

    client は boto3 transcribe client（テストは fake を注入）。存在すれば Update、
    無ければ Create（DG-C1-2）。FAILED は RuntimeError（FailureReason 付き）。
    """
    exists = _vocabulary_exists(client, name)
    if exists:
        print(f"既存語彙を更新します: {name}")
        client.update_vocabulary(VocabularyName=name, LanguageCode=language, VocabularyFileUri=file_uri)
    else:
        print(f"語彙を新規作成します: {name}")
        client.create_vocabulary(VocabularyName=name, LanguageCode=language, VocabularyFileUri=file_uri)

    deadline = monotonic() + poll_timeout_sec
    backoff = _INITIAL_BACKOFF_SEC
    while True:
        resp = client.get_vocabulary(VocabularyName=name)
        state = str(resp.get("VocabularyState", ""))
        if state == "READY":
            print(f"語彙が READY になりました: {name}")
            return state
        if state == "FAILED":
            reason = str(resp.get("FailureReason", "(理由不明)"))
            raise RuntimeError(f"語彙の処理が FAILED で終了しました（{name}）: {reason}")
        if monotonic() >= deadline:
            raise RuntimeError(
                f"語彙が {poll_timeout_sec} 秒以内に READY になりませんでした（{name}, state={state}）。"
            )
        print(f"処理中... (state={state})")
        sleeper(backoff)
        backoff = min(backoff * 2, _MAX_BACKOFF_SEC)


def _vocabulary_exists(client: Any, name: str) -> bool:
    try:
        client.get_vocabulary(VocabularyName=name)
        return True
    except Exception as exc:  # noqa: BLE001 — NotFound 以外は呼び出し側へ伝播させたいが、ここでは存在判定のみ
        if _is_not_found(exc):
            return False
        raise


def _is_not_found(exc: Exception) -> bool:
    # botocore ClientError は response['Error']['Code'] を持つ。fake では属性無しもありうる。
    response = getattr(exc, "response", None)
    code = response.get("Error") if isinstance(response, dict) else None
    if isinstance(code, dict):
        return code.get("Code") in {"NotFoundException", "BadRequestException"}
    return "not" in str(exc).lower() and "found" in str(exc).lower()


def _build_clients(region: str) -> tuple[Any, Any]:
    try:
        import boto3  # 遅延 import（テストでは注入で回避）
    except ImportError as exc:  # pragma: no cover
        print("boto3 が見つかりません。`uv run --with boto3 ...` で実行してください。", file=sys.stderr)
        raise SystemExit(2) from exc
    return boto3.client("transcribe", region_name=region), boto3.client("s3", region_name=region)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Transcribe カスタム語彙の冪等 upsert（C1）。")
    parser.add_argument("--name", required=True, help="語彙名（両ユニット設定と一致させる）。")
    parser.add_argument("--file", default="tools/vocab/ja-terms.txt", help="用語ファイル（table .txt）。")
    parser.add_argument("--bucket", required=True, help="用語ファイルの一時 upload 先 S3 バケット。")
    parser.add_argument("--key-prefix", default="subtext/vocab/", help="S3 キー接頭辞。")
    parser.add_argument("--region", default="ap-northeast-1", help="Transcribe/S3 の region。")
    parser.add_argument("--language", default="ja-JP", help="語彙の言語コード。")
    args = parser.parse_args(argv)

    file_path = Path(args.file)
    if not file_path.is_file():
        print(f"用語ファイルが見つかりません: {file_path}", file=sys.stderr)
        return 2

    text = file_path.read_text(encoding="utf-8")
    result = validate_table(text)
    if not result.ok:
        print("用語ファイルの検証に失敗しました:", file=sys.stderr)
        for err in result.errors:
            print(f"  - {err}", file=sys.stderr)
        return 2
    print(f"用語ファイル検証 OK（{result.phrase_count} 語）。")

    transcribe, s3 = _build_clients(args.region)
    prefix = args.key_prefix.strip("/")
    key = f"{prefix}/{args.name}.txt" if prefix else f"{args.name}.txt"
    try:
        s3.upload_file(str(file_path), args.bucket, key)
        file_uri = f"s3://{args.bucket}/{key}"
        print(f"S3 へアップロードしました: {file_uri}")
        upsert_vocabulary(transcribe, name=args.name, language=args.language, file_uri=file_uri)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — AWS 例外は原文を握り潰さず提示（NFR-C1-03）
        print(f"AWS 操作でエラーが発生しました: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
