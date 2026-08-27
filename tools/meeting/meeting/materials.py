"""付帯資料の投入（③付帯資料）。

複数ファイルをまとめて `data/out/<session>/materials/` へコピーする（Unit B の抽出段の入力）。
入力は複数行の文字列で受け、1行につき次のいずれかとして解釈する。

- 1つのファイルパス
- フォルダパス（直下の対応拡張子ファイルを全て採用。再帰しない）
- glob パターン（`*.pptx` 等）

対応拡張子は Unit B（`subtext_postmeeting.materials`）と同じ `.txt`／`.md`／`.pdf`／`.pptx` に揃える。
実際の抽出・上限判定・キャッシュは Unit B が担い、ここはファイルを `materials/` へ置くだけに
留める（FR-16：連携はファイルの受け渡しのみ）。
"""

from __future__ import annotations

import glob as glob_mod
import shutil
from pathlib import Path

from meeting.config import MeetingConfig

SUPPORTED_SUFFIXES = frozenset({".txt", ".md", ".pdf", ".pptx"})


def parse_input_lines(raw: str) -> tuple[str, ...]:
    """複数行入力を空行除去済みの行タプルへ分解する（純粋）。"""
    return tuple(line.strip() for line in raw.splitlines() if line.strip())


def resolve_paths(lines: tuple[str, ...]) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    """入力行（ファイル／フォルダ／glob）をファイルパスへ展開する（純粋・ファイル存在確認のみ）。

    戻り値は (対応形式のファイル一覧・重複ファイル名なし, 警告メッセージ一覧)。
    未対応拡張子・該当なし・重複ファイル名は黙って無視せず警告として残す。
    """
    resolved: list[Path] = []
    warnings: list[str] = []
    seen_names: set[str] = set()

    for line in lines:
        candidates = _expand(line)
        if not candidates:
            warnings.append(f"該当するファイルがありません: {line}")
            continue
        for path in candidates:
            if path.suffix.lower() not in SUPPORTED_SUFFIXES:
                warnings.append(f"未対応の形式のため無視します: {path.name}")
                continue
            if path.name in seen_names:
                warnings.append(f"同名ファイルが重複しています（先に見つかった方を採用）: {path.name}")
                continue
            seen_names.add(path.name)
            resolved.append(path)
    return tuple(resolved), tuple(warnings)


def _expand(line: str) -> list[Path]:
    candidate = Path(line)
    if candidate.is_dir():
        return sorted(p for p in candidate.iterdir() if p.is_file())
    if candidate.is_file():
        return [candidate]
    matched = sorted(Path(m) for m in glob_mod.glob(line))
    return [m for m in matched if m.is_file()]


def copy_into(cfg: MeetingConfig, session: str, paths: tuple[Path, ...]) -> Path:
    """`paths` を `data/out/<session>/materials/` へコピーする（同名は上書き）。

    戻り値は materials/ ディレクトリ。実際の抽出・キャッシュは Unit B 側（次回 `meeting minutes`
    実行時）に委ねる。
    """
    materials_dir = cfg.session_out_dir(session) / "materials"
    materials_dir.mkdir(parents=True, exist_ok=True)
    for path in paths:
        shutil.copy2(path, materials_dir / path.name)
    return materials_dir
