"""付帯資料の取込（③付帯資料・FR-MAT）。

会議資料（`.txt`／`.md`／`.pdf`／`.pptx`）をローカルで抽出し、要約プロンプトへ `[参考資料]` として
渡す。抽出そのものは非課金・ローカル処理のみ（画像・スキャン PDF の vision 抽出は対象外＝
将来対応）。用途の限定・出典付記・資料を記述根拠にしない不変条件は BR-MAT-01（summarize.py の
プロンプト側で強制する）。

責務分離（FR-11 と同じ原則）:
  - 抽出（`extract_one`）は 1 ファイルを読んで `MaterialText` を返す純粋に近い変換（ファイル IO は
    持つが外部状態には依存しない、同一ファイルは同一結果）。
  - 集約（`collect`）は複数ファイルを束ね、文字数上限に基づく除外を決定する純粋関数。
  - 差分検知（`load_cache`/`needs_extraction`）はキャッシュ（`materials.extracted.json`）との
    比較のみを行い、抽出をやり直すかどうかを判定する（BR-MAT-02: 変更の無いファイルは
    再抽出しない）。

対応拡張子ごとの制約:
  - `.txt`／`.md`：UTF-8 → CP932 の順でデコードを試す。
  - `.pdf`：`pypdf` でテキスト層のみ抽出する。テキスト層が空（スキャン PDF・画像のみ）の場合は
    0 文字で通さず対象外として警告する（黙って0文字を受理すると「資料を渡したのに反映されない」
    ことの原因が分からなくなる）。
  - `.pptx`：`python-pptx` でスライドのテキストフレームと発表者ノートを抽出する。図中の文字
    （画像として貼られたテキスト）は取得できない。
  - 上記以外の拡張子は対象外として警告する（黙って無視しない）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import PipelineError

_STAGE = "materials"
_SUPPORTED_SUFFIXES = frozenset({".txt", ".md", ".pdf", ".pptx"})
_ENCODINGS = ("utf-8", "cp932")


@dataclass(frozen=True)
class MaterialText:
    """1ファイルぶんの抽出結果。"""

    file_name: str
    kind: str  # 拡張子（先頭ドット無し。小文字）
    extraction_method: str  # "text" / "pdf-text-layer" / "pptx" のいずれか
    content_hash: str  # 元ファイルの SHA-256（差分検知に使う）
    text: str

    @property
    def char_count(self) -> int:
        return len(self.text)

    def to_json(self) -> dict[str, Any]:
        return {
            "fileName": self.file_name,
            "kind": self.kind,
            "extractionMethod": self.extraction_method,
            "contentHash": self.content_hash,
            "charCount": self.char_count,
            "text": self.text,
        }

    @staticmethod
    def from_json(data: dict[str, Any]) -> "MaterialText":
        return MaterialText(
            file_name=str(data["fileName"]),
            kind=str(data["kind"]),
            extraction_method=str(data["extractionMethod"]),
            content_hash=str(data["contentHash"]),
            text=str(data.get("text", "")),
        )


@dataclass(frozen=True)
class MaterialsResult:
    """`collect` の結果。`included` は要約プロンプトへ渡す資料、`excluded` は上限超過等で除外した
    資料（ファイル名と理由）。`warnings` は未対応拡張子・抽出不能等の案件別メッセージ。

    `fingerprints` は**見たファイルすべて**（採用・除外・抽出不能を問わない）の
    (ファイル名, 内容ハッシュ) である。差分検知（`needs_extraction`）はこれだけを見る。
    採用分だけを台帳にすると、抽出できなかったファイル（スキャン PDF・未対応拡張子）が
    materials/ に居座るたびに「未知のファイルがある」と判定され続け、要約段の再実行＝
    **Bedrock 再課金が実行のたびに起きる**（FR-MAT-02 / FR-MAT-06 の前提）。
    ハッシュを取れなかったファイルは空文字を持ち、存在の有無だけで比較する。
    """

    included: tuple[MaterialText, ...] = ()
    excluded: tuple[tuple[str, str], ...] = ()  # (file_name, reason)
    warnings: tuple[str, ...] = ()
    fingerprints: tuple[tuple[str, str], ...] = ()  # (file_name, content_hash)

    def to_json(self) -> dict[str, Any]:
        return {
            "materials": [m.to_json() for m in self.included],
            "excluded": [{"fileName": name, "reason": reason} for name, reason in self.excluded],
            "warnings": list(self.warnings),
            "fingerprints": [{"fileName": name, "contentHash": digest} for name, digest in self.fingerprints],
        }

    @staticmethod
    def from_json(data: dict[str, Any]) -> "MaterialsResult":
        included = tuple(MaterialText.from_json(m) for m in data.get("materials", []))
        excluded = tuple((str(e["fileName"]), str(e["reason"])) for e in data.get("excluded", []))
        raw_fingerprints = data.get("fingerprints")
        if raw_fingerprints is None:
            # `fingerprints` を持たない旧形式のキャッシュ。採用分はハッシュを流用し、除外分は
            # 存在のみ（空ハッシュ）で引き継ぐ。無いものとして扱うと、既存セッションの初回実行が
            # 一律で再抽出＝再要約（課金）になるため橋を架ける。
            fingerprints = tuple(
                [(m.file_name, m.content_hash) for m in included] + [(name, "") for name, _ in excluded]
            )
        else:
            fingerprints = tuple(
                (str(f["fileName"]), str(f.get("contentHash", ""))) for f in raw_fingerprints
            )
        return MaterialsResult(
            included=included,
            excluded=excluded,
            warnings=tuple(str(w) for w in data.get("warnings", [])),
            fingerprints=fingerprints,
        )

    def is_empty(self) -> bool:
        return not self.included and not self.excluded and not self.warnings


def content_hash(path: Path) -> str:
    """ファイル内容の SHA-256（差分検知用）。

    `hashlib.file_digest` は内部でブロック単位に読むため、ファイル全量をメモリへ載せない。
    materials/ には誤って巨大なファイル（動画等）が置かれ得るので、読み方をここで固定する。
    """
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def _digest_bytes(data: bytes) -> str:
    """既に読み込んだバイト列の SHA-256（同じファイルを二度読まないため）。"""
    return hashlib.sha256(data).hexdigest()


def _safe_content_hash(path: Path) -> str:
    """`content_hash` の失敗許容版（読めなければ空文字）。

    差分検知の台帳は「見たファイルすべて」を載せるため、抽出できないファイル（権限・共有ロック・
    読取エラー）でも名前だけは記録できるようにする。空ハッシュは「内容不明」を意味し、
    比較は存在の有無だけで行う。
    """
    try:
        return content_hash(path)
    except OSError:
        return ""


def extract_one(path: Path, *, content_digest: str | None = None) -> MaterialText:
    """1ファイルを抽出する。未対応拡張子・抽出不能は `PipelineError`（呼び出し側が警告に変換する）。

    `content_digest` を渡すと内容ハッシュの再計算を省く（`collect` が差分検知用に先に計算する）。
    """
    suffix = path.suffix.lower()
    if suffix not in _SUPPORTED_SUFFIXES:
        raise PipelineError(f"未対応の資料形式です（対象外）: {path.name}", failed_stage=_STAGE)

    if suffix in (".txt", ".md"):
        # テキストは 1 回の読取でハッシュと本文の両方を得る（ハッシュのために読み直さない）。
        data = _read_bytes(path)
        return MaterialText(
            file_name=path.name,
            kind=suffix.lstrip("."),
            extraction_method="text",
            content_hash=content_digest or _digest_bytes(data),
            text=_decode(data, path.name),
        )

    # pdf/pptx は解析ライブラリがファイルを自ら読むため、ハッシュは逐次読みで別に取る。
    digest = content_digest or content_hash(path)
    if suffix == ".pdf":
        text = _extract_pdf_text(path)
        if not text.strip():
            raise PipelineError(
                f"テキスト層が空です（スキャン PDF・画像のみの可能性）。対象外にします: {path.name}",
                failed_stage=_STAGE,
            )
        return MaterialText(
            file_name=path.name, kind="pdf", extraction_method="pdf-text-layer", content_hash=digest, text=text
        )
    # .pptx
    return MaterialText(
        file_name=path.name,
        kind="pptx",
        extraction_method="pptx",
        content_hash=digest,
        text=_extract_pptx_text(path),
    )


def _read_bytes(path: Path) -> bytes:
    """テキスト資料の生バイト列（読取失敗は actionable な PipelineError）。"""
    try:
        return path.read_bytes()
    except OSError as exc:
        raise PipelineError(f"資料を読み込めません: {path.name}", failed_stage=_STAGE) from exc


def _decode(data: bytes, name: str) -> str:
    """テキスト資料をデコードする（UTF-8 → CP932 の順・純粋）。"""
    for encoding in _ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise PipelineError(f"文字コードを判定できません（UTF-8/CP932 以外）: {name}", failed_stage=_STAGE)


def _extract_pdf_text(path: Path) -> str:
    from pypdf import PdfReader  # 遅延 import（本形式を使わない場合の起動コスト回避）

    try:
        reader = PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001 pypdf は多様な例外を投げる（壊れたPDF・暗号化等）
        raise PipelineError(f"PDF の読込に失敗しました: {path.name}: {exc}", failed_stage=_STAGE) from exc
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages)


def _extract_pptx_text(path: Path) -> str:
    from pptx import Presentation  # 遅延 import

    try:
        presentation = Presentation(str(path))
    except Exception as exc:  # noqa: BLE001 python-pptx も多様な例外を投げる
        raise PipelineError(f"pptx の読込に失敗しました: {path.name}: {exc}", failed_stage=_STAGE) from exc

    chunks: list[str] = []
    for i, slide in enumerate(presentation.slides, start=1):
        slide_lines = [f"[スライド{i}]"]
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                slide_lines.append(shape.text_frame.text)
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                slide_lines.append(f"[ノート] {notes}")
        if len(slide_lines) > 1:
            chunks.append("\n".join(slide_lines))
    return "\n\n".join(chunks)


def collect(paths: tuple[Path, ...], *, max_total_chars: int) -> MaterialsResult:
    """複数ファイルを抽出し、文字数上限で除外を決める（純粋に近い集約）。

    上限超過は**ファイル単位**で末尾（引数の順序の後方）から除外する。1ファイルの途中で切ると
    「半分だけ真実」の資料になり、表記是正・出典付記の判断材料としてかえって悪い。
    """
    included: list[MaterialText] = []
    excluded: list[tuple[str, str]] = []
    warnings: list[str] = []
    fingerprints: list[tuple[str, str]] = []
    total = 0

    for path in paths:
        # 抽出の成否に関わらず「見た」ことを記録する（差分検知の母集団。MaterialsResult 参照）。
        # 成功時は抽出が算出したハッシュを流用し、同じファイルを二度ハッシュしない。
        try:
            material = extract_one(path)
        except PipelineError as exc:
            warnings.append(str(exc))
            fingerprints.append((path.name, _safe_content_hash(path)))
            continue
        fingerprints.append((material.file_name, material.content_hash))
        if total + material.char_count > max_total_chars:
            excluded.append((material.file_name, f"文字数上限（{max_total_chars}）を超えるため除外"))
            continue
        included.append(material)
        total += material.char_count

    return MaterialsResult(
        included=tuple(included),
        excluded=tuple(excluded),
        warnings=tuple(warnings),
        fingerprints=tuple(fingerprints),
    )


def format_for_prompt(result: MaterialsResult) -> str:
    """`[参考資料]` ブロックを組み立てる（純粋）。資料が無ければ空文字列を返す。"""
    if not result.included:
        return ""
    blocks = [
        f"--- 資料{i}: {m.file_name}（{m.char_count}文字）---\n{m.text}"
        for i, m in enumerate(result.included, start=1)
    ]
    return "[参考資料]\n" + "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# キャッシュ（materials.extracted.json）との差分検知
# ---------------------------------------------------------------------------
def load_cache(path: Path) -> MaterialsResult | None:
    """`materials.extracted.json` を読む。未存在なら None。壊れていれば actionable エラー。"""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"materials.extracted.json の読込に失敗しました: {path}: {exc}", failed_stage=_STAGE) from exc
    return MaterialsResult.from_json(data)


def save_cache(path: Path, result: MaterialsResult) -> None:
    try:
        path.write_text(json.dumps(result.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        raise PipelineError(f"materials.extracted.json の書込に失敗しました: {path}: {exc}", failed_stage=_STAGE) from exc


def needs_extraction(materials_dir: Path, cache: MaterialsResult | None) -> bool:
    """`materials_dir` の内容がキャッシュと一致しないか（再抽出が必要か）を判定する（純粋に近い）。

    キャッシュが無ければ常に True。ファイルの追加・削除・内容変更（`content_hash` 不一致）の
    いずれかがあれば True。比較の母集団は `fingerprints`（見たファイルすべて）で、抽出できなかった
    ファイルも含む。採用分だけを見ると、スキャン PDF 等が materials/ に残っている間ずっと
    「未知のファイルがある」と判定され、要約段の再実行＝Bedrock 再課金が毎回起きる。
    内容ハッシュが空（読取不能だったファイル）は存在の有無だけで比較する。
    """
    if cache is None:
        return True
    recorded = dict(cache.fingerprints)
    if not materials_dir.is_dir():
        return bool(recorded)  # 資料が消えたのに前回結果が残っている
    current = {p.name: p for p in materials_dir.iterdir() if p.is_file()}
    if set(current) != set(recorded):
        return True
    return any(
        recorded[name] and _safe_content_hash(path) != recorded[name] for name, path in current.items()
    )
