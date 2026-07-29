"""話者名ゲート（BR-NAME-01/02）の記入対象抽出と書き戻し（純粋ロジック + ファイル IO）。

`meeting process`（wizard.py・ターミナルの対話入力）と `meeting tui`（tui.py・モーダル入力）が
同じ規則で `speaker_names.json` を扱うために、入力手段に依存しない部分だけをここへ集める。

方針:
  - 空欄（空文字/空白のみ）の others ラベル、および **自動補完されただけで人が確認していない**
    ラベル（`_hintedLabels`）を記入対象とし、確認済みの実名と `self` は温存する（BR-NAME-02）。
    既定「自分」を人手の記入で上書きしない。
  - 書き戻しは非破壊（新しい dict を返す）。`_clusters` など未知のキーもそのまま持ち越す。
  - 実名(PII)の表示・ログ出力はここでは行わず、呼び出し側の UI 層に委ねる（BR-NAME-04）。

「値が入っている＝確認済み」としないのは、VTT/ライブ字幕由来のヒントが実名とは限らないため
（ライブ字幕は「自分」「相手」、Teams は「Speaker 1」のような仮名を出す）。値の有無だけで
ゲートを通すと、参加者全員が仮名 1 人に統合された議事録が黙って出来上がる。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

# speaker_names.json の self ラベル（Unit B が既定名「自分」を入れる。記入対象から除く）。
SELF_LABEL = "self"
# Unit B（speaker_label.build_template）が自動補完したラベルの記録キー。人の確認を経ていない印。
HINTED_LABELS_KEY = "_hintedLabels"
# 発話数が取れなかった場合の表示（`_clusters` が無い古い出力や手書きファイル向け）。
_UNKNOWN_COUNT = "?"


@dataclass(frozen=True)
class SpeakerPrompt:
    """記入待ちラベル1件ぶんの提示材料（「誰か」を判別する手がかり）。

    `samples` は代表発言で PII を含みうるため、操作者への画面表示のみに使う（BR-NAME-04）。
    `current` は自動補完された候補（空なら候補なし）。UI は初期値として提示し、操作者が
    そのまま確定するか補正するかを選べるようにする。
    """

    label: str
    segment_count: int | str
    samples: tuple[str, ...]
    current: str = ""


def load(path: Path) -> dict[str, Any]:
    """speaker_names.json を読む。壊れていれば actionable な ValueError を送出する。"""
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"speaker_names.json の読込に失敗しました: {path}: {exc}") from exc
    return data


def save(path: Path, data: Mapping[str, Any]) -> None:
    """speaker_names.json を書き戻す（Unit B が読める整形・非 ASCII をそのまま保つ）。"""
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"speaker_names.json の書込に失敗しました: {path}: {exc}") from exc


def pending_prompts(data: Mapping[str, Any]) -> list[SpeakerPrompt]:
    """記入待ちラベルを `mappings` の並び順で返す（空欄、または未確認の自動補完）。

    `_clusters`（Unit B の SpeakerCluster.to_json()・camelCase）から発話数と代表発言を引く。
    対応するクラスタが無いラベルも記入対象には残す（手がかりが無いだけで記入は必要）。
    `_hintedLabels` に載っているラベルは値が入っていても「人が確認していない候補」として
    記入対象に含める（モジュール docstring 参照）。
    """
    # キーが存在しても JSON null の場合があるため `or` でフォールバック（dict(None) 等を防ぐ）。
    mappings = data.get("mappings") or {}
    clusters = {str(c.get("label")): c for c in (data.get("_clusters") or [])}
    hinted = _hinted_labels(data)

    prompts: list[SpeakerPrompt] = []
    for label, name in mappings.items():
        if label == SELF_LABEL:
            continue
        current = str(name or "").strip()
        if current and str(label) not in hinted:
            continue  # 人が確認済みの実名は触らない（BR-NAME-02）
        cluster = clusters.get(label, {})
        samples = tuple(str(s) for s in (cluster.get("sampleUtterances") or []))
        prompts.append(
            SpeakerPrompt(
                label=str(label),
                segment_count=cluster.get("segmentCount", _UNKNOWN_COUNT),
                samples=samples,
                current=current,
            )
        )
    return prompts


def apply_names(data: Mapping[str, Any], names: Mapping[str, str]) -> dict[str, Any]:
    """記入待ちラベルへ実名を当てた新しい dict を返す（非破壊, BR-NAME-02）。

    反映するのは記入待ちラベルのみ。既確認のラベルや `self` への指定は無視する（人手の誤操作で
    既存名を壊さないため）。自動補完された候補を持つラベルは、空入力を「候補のまま確定する」と
    解釈して値を書き、`_hintedLabels` から外す（＝以後は確認済みとして扱い、再度ゲートで
    止めない）。候補が無いラベルの空入力は従来どおりスキップし、元の spk_n を維持する。
    """
    mappings = dict(data.get("mappings") or {})
    hinted = _hinted_labels(data)
    pending = {p.label: p.current for p in pending_prompts(data)}
    for label, name in names.items():
        if label not in pending:
            continue
        cleaned = name.strip() or pending[label]
        if not cleaned:
            continue
        mappings[label] = cleaned
        hinted.discard(label)
    updated = {**data, "mappings": mappings}
    if hinted:
        updated[HINTED_LABELS_KEY] = sorted(hinted)
    else:
        updated.pop(HINTED_LABELS_KEY, None)
    return updated


def applied_count(data: Mapping[str, Any], names: Mapping[str, str]) -> int:
    """`apply_names` で実際に反映される件数（UI の件数表示用。実名は返さない, BR-NAME-04）。"""
    pending = {p.label: p.current for p in pending_prompts(data)}
    return sum(1 for label, name in names.items() if label in pending and (name.strip() or pending[label]))


def _hinted_labels(data: Mapping[str, Any]) -> set[str]:
    """自動補完されたまま人の確認を経ていないラベル集合（無ければ空集合）。"""
    return {str(label) for label in (data.get(HINTED_LABELS_KEY) or [])}
