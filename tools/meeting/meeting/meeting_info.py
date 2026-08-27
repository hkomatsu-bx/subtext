"""会議情報（会議名・日時・参加者）の記入対象抽出と書き戻し（FR-MI-01）。

`speaker_names.json`（naming.py）と同じファイル方式・非対話原則を踏む。話者名ゲートと
同じ停止点（NAMING_REQUIRED）で続けて入力を訊く。全項目が任意で、未記入なら
Unit B 側（subtext_postmeeting.summarize）が既存の決定的算出へフォールバックする（BR-MI-01）。

参加者名は PII を含み得るため、ログ出力はここでは行わず呼び出し側の UI 層に委ねる（BR-NAME-04）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def path_for(out_dir: Path) -> Path:
    return out_dir / "meeting_info.json"


def load(path: Path) -> dict[str, Any]:
    """meeting_info.json を読む。未存在なら空の記入待ちテンプレを返す（壊れていれば actionable エラー）。"""
    if not path.is_file():
        return {"title": "", "datetime": "", "participants": []}
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"meeting_info.json の読込に失敗しました: {path}: {exc}") from exc
    return data


def save(path: Path, data: dict[str, Any]) -> None:
    """meeting_info.json を書き戻す（Unit B が読める整形・非 ASCII をそのまま保つ）。"""
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"meeting_info.json の書込に失敗しました: {path}: {exc}") from exc


def is_empty(data: dict[str, Any]) -> bool:
    """3項目すべて未記入かどうか（純粋）。"""
    title = str(data.get("title") or "").strip()
    meeting_datetime = str(data.get("datetime") or "").strip()
    participants = [str(p).strip() for p in data.get("participants") or [] if str(p).strip()]
    return not title and not meeting_datetime and not participants


def build(title: str, meeting_datetime: str, participants_line: str) -> dict[str, Any]:
    """対話入力（1行のカンマ/読点区切り参加者）から meeting_info.json の内容を組み立てる（純粋）。

    空行のみの入力はそのフィールドを未記入として扱う（既存値の消去とは区別しない。
    会議情報は articulate な誤記入より「入力を省略できる」ことを優先する）。
    """
    parts = [p.strip() for p in participants_line.replace("、", ",").split(",") if p.strip()]
    return {"title": title.strip(), "datetime": meeting_datetime.strip(), "participants": parts}


def unmatched_participants(data: dict[str, Any], resolved_speaker_names: tuple[str, ...]) -> tuple[str, ...]:
    """話者名ゲートで実名解決済みなのに参加者リストに無い名前を返す（記入漏れの可能性、警告のみ）。

    逆方向（参加者リストにあるが発言していない人）は検出しない。発言しなかった参加者を
    記録することが会議情報挿入の目的そのものであるため。
    """
    participants = {str(p).strip() for p in data.get("participants") or [] if str(p).strip()}
    return tuple(name for name in resolved_speaker_names if name and name not in participants)
