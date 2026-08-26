"""後処理ウィザード（meeting process）。

録音済セッションから minutes.md までを、対話1コマンドで駆動する。手作業の往復
（minutes 実行 → 話者名を外部エディタで記入 → minutes 再実行）を1本にまとめ、
「複数コマンド」「次の段の失念」という UX 上の摩擦を解消する。

方針:
  - 段検知（runner.detect_stage）で現在地を提示し、ファイル存在に基づいて自動前進する。
  - 話者名ゲート（NAMING_REQUIRED）は speaker_names.json の `_clusters`（代表発言サンプル）
    を提示し、その場で実名を入力させて空欄の mappings のみ書き戻す（既記入・self は温存）。
  - 実際のパイプライン実行・課金・台帳記録は呼び出し側が注入する `run_minutes`（cli 側の
    _run_minutes_pipeline）に委ね、ここではオーケストレーションに専念する（責務分離）。
  - Slack 投稿は任意フック（slack_offer）として cli から注入され、minutes.md 完成後に提案する
    （外部公開＝投稿前にプレビューと y/N 承認を必須とする。実体は cli._offer_slack_post）。
  - 実名(PII)は画面表示のみ（操作者向け）で logging へは出さない（BR-NAME-04 と整合）。
"""

from __future__ import annotations

from typing import Callable

from meeting import meeting_info, naming, runner
from meeting.config import MeetingConfig
from meeting.runner import Stage

# 対話入力の seam（テストで差し替え）。プロンプト文字列を受け取り1行返す（input 互換）。
Ask = Callable[[str], str]
# ユーザー向け出力の seam（既定 print。テストは capsys で捕捉する）。
Emit = Callable[[str], None]
# パイプライン実行1回の seam。セッションIDを受け取り終了コードを返す。
RunMinutes = Callable[[str], int]
# 議事録完成後に任意で Slack 投稿を提案するフック（cli が注入。None なら提案しない）。
SlackOffer = Callable[[str], None]

# パイプライン実行回数の上限。通常 RECORDED→NAMING_REQUIRED→MINUTES_DONE の2回で完了する。
# 進捗しない異常時に無限ループへ陥らないための保険（3回目以降は打ち切る）。
_MAX_PIPELINE_RUNS = 3


def run(
    cfg: MeetingConfig,
    session: str,
    *,
    claude: bool,
    run_minutes: RunMinutes,
    ask: Ask = input,
    emit: Emit = print,
    slack_offer: SlackOffer | None = None,
) -> int:
    """後処理ウィザードの本体。session は解決済みを受け取る（呼び出し側で resolve 済み）。

    minutes.md 到達で 0（`slack_offer` があれば投稿を提案）。Claude 生成経路は
    final_transcript.json で停止し案内して 0。パイプラインが異常終了/無進捗なら非0。
    """
    emit(f"== 後処理ウィザード: {session} ==")
    emit(f"  現在段: {runner.STAGE_LABELS[runner.detect_stage(cfg, session)]}")

    runs = 0
    while True:
        stage = runner.detect_stage(cfg, session)

        if stage is Stage.MINUTES_DONE:
            _finish(cfg, session, emit, slack_offer)
            return 0

        if stage is Stage.TRANSCRIPT_DONE and claude:
            # Claude 生成経路の停止点。議事録は Claude が final_transcript.json から生成する。
            emit("Claude 生成経路: final_transcript.json まで完了しました。")
            emit(f"  data/out/{session}/final_transcript.json を Claude に読ませて minutes.md を生成してください。")
            return 0

        if runs >= _MAX_PIPELINE_RUNS:
            emit("想定回数内に議事録が完成しませんでした。`meeting status` で確認してください。")
            return 1

        if stage is Stage.NAMING_REQUIRED:
            _prompt_speaker_names(cfg, session, ask, emit)
            _prompt_meeting_info(cfg, session, ask, emit)

        before = stage
        rc = run_minutes(session)
        runs += 1
        if rc != 0:
            return rc

        # パイプラインが段を進めなければ打ち切る（無進捗の異常検知）。
        if runner.detect_stage(cfg, session) is before:
            emit("パイプラインが進捗しませんでした。ログを確認してください。")
            return 1


def _finish(cfg: MeetingConfig, session: str, emit: Emit, slack_offer: SlackOffer | None) -> None:
    """MINUTES_DONE 到達時の最終処理: 議事録表示 → 任意で Slack 投稿を提案（BR-NAME-04 と整合）。"""
    _show_minutes(cfg, session, emit)
    if slack_offer is not None:
        slack_offer(session)
    emit("完了。")


def _prompt_speaker_names(cfg: MeetingConfig, session: str, ask: Ask, emit: Emit) -> None:
    """speaker_names.json の空欄ラベルを対話入力で埋める（BR-NAME-01/02）。

    記入対象の抽出と書き戻しの規則は `naming` へ委ね（TUI のモーダル入力と共通）、ここは
    ターミナルでの提示と入力収集に専念する。Enter（空入力）はスキップし元のラベルを維持する。
    """
    path = cfg.session_out_dir(session) / "speaker_names.json"
    if not path.is_file():
        # 段検知と不整合な場合は黙って戻る（再実行で再生成される）。
        return

    data = naming.load(path)
    prompts = naming.pending_prompts(data)
    if not prompts:
        return

    emit("話者名の記入（候補があれば Enter でそのまま確定。候補が無い空欄は元のラベルのまま残ります）:")
    names: dict[str, str] = {}
    for prompt in prompts:
        emit(f"  [{prompt.label}] 発話数 {prompt.segment_count} / 代表発言:")
        for sample in prompt.samples:
            emit(f"      「{sample}」")
        # 候補（VTT/ライブ字幕由来）は仮名であり得るため、そのまま確定しないよう値を見せて問う。
        suffix = f"（候補: {prompt.current}）" if prompt.current else ""
        names[prompt.label] = ask(f"  {prompt.label} の実名{suffix} > ")

    if naming.applied_count(data, names) == 0:
        emit("  （記入なし。ラベルのまま続行します）")
        return

    naming.save(path, naming.apply_names(data, names))
    emit("  speaker_names.json を更新しました。")


def _prompt_meeting_info(cfg: MeetingConfig, session: str, ask: Ask, emit: Emit) -> None:
    """meeting_info.json への会議名・日時・参加者の記入を訊く（FR-MI-01）。

    話者名ゲートと同じ停止点で続けて訊く。全項目任意で、未存在時のみ訊く
    （2回目以降の実行で毎回訊かれると煩わしいため。修正したい場合はファイルを直接編集する）。
    """
    path = meeting_info.path_for(cfg.session_out_dir(session))
    if path.is_file():
        return

    emit("会議情報の記入（すべて任意。Enter でスキップ）:")
    title = ask("  会議名 > ")
    meeting_datetime = ask("  日時 > ")
    participants_line = ask("  参加者（読点またはカンマ区切り） > ")
    data = meeting_info.build(title, meeting_datetime, participants_line)
    meeting_info.save(path, data)
    if meeting_info.is_empty(data):
        emit("  （記入なし。既存の算出値のまま議事録を生成します）")
    else:
        emit("  meeting_info.json を保存しました。")


def _show_minutes(cfg: MeetingConfig, session: str, emit: Emit) -> None:
    """生成された minutes.md を画面に表示する（レビュー用）。"""
    path = cfg.session_out_dir(session) / "minutes.md"
    emit(f"== 議事録: data/out/{session}/minutes.md ==")
    try:
        emit(path.read_text(encoding="utf-8"))
    except OSError as exc:
        emit(f"（minutes.md を読み込めませんでした: {exc}）")
