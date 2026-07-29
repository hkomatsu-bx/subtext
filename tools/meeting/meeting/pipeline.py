"""議事録パイプラインの実行ガバナンス（見積→自動実行→台帳追記→閾値警告）。

`meeting minutes`/`meeting process`（cli.py）と `meeting tui`（tui.py）から共用する。出力は
emit（既定 print）へ委譲し、呼び出し側が表示先（stdout / TUI ログ）を選べるようにする。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, NamedTuple

from meeting import ledger, runner
from meeting.config import MeetingConfig
from meeting.ledger import LedgerEntry
from meeting.pricing import Estimate
from meeting.runner import Runner as SubprocessRunner
from meeting.runner import Stage

# ユーザー向け出力の seam（既定 print。TUI はログウィジェットへの書込に差し替える）。
Emit = Callable[[str], None]

# 台帳の read-modify-write（load→cumulative_after→append）を跨ぐ排他ロック。TUI は minutes/vtt を
# 別スレッドで並行実行し得るため、ロック無しだと双方が同じ古い月次合計から cumulativeMonthUsd を
# 計算して累計を取りこぼす。プロセス内の直列化に限る（ledger.py は追記のみで自前ロックを持たない）。
_LEDGER_LOCK = threading.Lock()

# 異常終了時の注記。成果物が残らなかった段（例: Transcribe ジョブは完了したが結果の取得で落ちた）は
# 痕跡から課金を判定できないため、台帳の欠けを黙らせず操作者に知らせる。
_PARTIAL_COST_WARNING = (
    "⚠ 課金段の途中で失敗した場合、発生済みの AWS 課金が台帳に載らないことがあります"
    "（成果物が残らなかった段は痕跡から判定できません）。正確な額は AWS Cost Explorer で確認してください。"
)


class _PendingCost(NamedTuple):
    """台帳へ未追記のコスト項目（見積・実測根拠量・単価）。"""

    estimate: Estimate
    units: dict[str, float]
    unit_price_usd: float


def run_minutes_pipeline(
    cfg: MeetingConfig,
    session: str,
    *,
    claude: bool,
    run: SubprocessRunner,
    emit: Emit = print,
) -> int:
    """議事録パイプラインを1回：見積提示→実行→台帳追記→閾値警告。rc を返す。

    次段の案内は行わない（呼び出し側の責務。`next_steps_lines` を使う）。
    """
    # 1) 事前見積（Transcribe は録音秒数から算出可能）と今月累計を提示。
    tr_est = runner.estimate_transcribe(cfg, session)
    month = ledger.month_of(ledger.now_iso())
    month_so_far = ledger.monthly_total(ledger.load(cfg.ledger_path), month)
    emit(f"== 議事録パイプライン: {session} ==")
    emit(f"  経路        : {'Claude 生成（PII 外部送信）' if claude else 'Bedrock 生成'}")
    emit(f"  今回見積    : {tr_est.detail} = ${tr_est.usd}")
    emit(
        f"  今月累計    : ${month_so_far} （上限 単発 ${cfg.thresholds.per_run_usd} / "
        f"月次 ${cfg.thresholds.monthly_usd}）"
    )

    # 2) 自動実行（課金境界。合意済: 実行可・ただし台帳必須）。
    emit("  パイプライン実行中（AWS 課金が発生します）...")
    result = runner.run_pipeline(cfg, session, claude=claude, runner=run)

    # 3) 実行済みの段を概算で台帳追記（金額は概算・正は Cost Explorer。再実行の二重計上は
    #    has_entry で防止）。実測課金額ではなく単価×実測根拠量（録音分/文字数）に基づく。
    #    **異常終了でも必ず通す**: 途中で落ちてもそこまでの AWS 課金は発生しており、記録しないと
    #    「課金するなら台帳必須」＝自動実行を許容している前提が破れる。幻の計上は record_costs 側で
    #    段ごとの痕跡を確認して避ける。
    per_run = _record_safely(emit, lambda: record_costs(cfg, session, claude, month))

    if result.returncode != 0:
        emit(f"パイプラインが異常終了しました (exit={result.returncode})")
        if result.stderr:
            emit(result.stderr.strip()[-2000:])
        emit(_PARTIAL_COST_WARNING)
        return result.returncode

    # 4) 閾値判定（警告のみ・停止しない）。
    month_total = ledger.monthly_total(ledger.load(cfg.ledger_path), month)
    for w in ledger.evaluate(per_run, month_total, cfg.thresholds):
        emit(f"⚠ コスト警告: {w}")
    return 0


def _record_safely(emit: Emit, record: Callable[[], float]) -> float:
    """台帳追記を実行し、追記自体が失敗しても呼び出し側の異常報告を潰さない。

    追記中の例外（セッションが削除された・manifest や台帳が壊れている等）をそのまま送出すると、
    本来報告すべきパイプラインの失敗理由が隠れる。ここでは警告に留めて rc を維持し、
    「記録できなかった」ことを操作者に見せる（黙って 0 円扱いにしない）。
    """
    try:
        return record()
    except (OSError, ValueError, KeyError) as exc:
        emit(f"⚠ コスト台帳へ追記できませんでした（AWS 課金が発生している可能性があります）: {exc}")
        return 0.0


def record_costs(cfg: MeetingConfig, session: str, claude: bool, month: str) -> float:
    """発生済みの課金を成果物から突き合わせ、未記録ぶんを台帳へ追記して単発合計を返す。

    根拠は「その段の成果物がある＝段が実行された＝課金された」（Transcribe=raw 出力 /
    C2 補正=final の補正メタ / 要約=minutes.md）。痕跡を見ずに無条件で計上すると、資格情報
    エラー等で課金前に落ちた実行にも幻の金額を積んでしまう。逆に成功時しか記録しないと、
    課金後に落ちた実行が台帳から消える。再実行の二重計上は has_entry で防ぐ（成果物を再利用
    する再実行では課金が発生しない, BR-PIPE-04）。
    """
    entries = ledger.load(cfg.ledger_path)
    pending = [
        *_pending_transcribe_cost(cfg, session, entries),
        *_pending_llm_costs(cfg, session, claude, entries),
    ]
    return _append_pending_costs(cfg, session, month, pending)


def record_summary_cost(cfg: MeetingConfig, session: str, claude: bool, month: str) -> float:
    """LLM 段（C2 補正・要約）のみの概算を台帳へ追記する（VTT/mp4 取込由来。FR-17: Transcribe 無し）。"""
    entries = ledger.load(cfg.ledger_path)
    pending = _pending_llm_costs(cfg, session, claude, entries)
    return _append_pending_costs(cfg, session, month, pending)


def _pending_transcribe_cost(
    cfg: MeetingConfig, session: str, entries: list[LedgerEntry]
) -> list[_PendingCost]:
    """Transcribe 段の未追記コストを返す（raw 出力があれば課金済み）。"""
    if not runner.transcribe_ran(cfg, session) or ledger.has_entry(entries, session, "transcribe"):
        return []
    manifest = runner.read_manifest(cfg, session)
    total_min = round((manifest.self_sec + manifest.others_sec) / 60.0, 2)
    return [
        _PendingCost(
            runner.estimate_transcribe(cfg, session),
            {"minutes": total_min},
            cfg.pricing.transcribe_usd_per_minute,
        )
    ]


def _pending_llm_costs(
    cfg: MeetingConfig, session: str, claude: bool, entries: list[LedgerEntry]
) -> list[_PendingCost]:
    """LLM 段（C2 補正・要約）の未追記コストを返す（record_costs/record_summary_cost 共通）。

    既定経路（Bedrock 生成）は **要約段の前に C2 用語補正段でも Bedrock を踏む** ため、2 段を
    別々に数える。Claude 経路は `--no-summarize` で補正段も自動スキップされるため補正メタが
    残らず、痕跡ベースの判定で自然に対象外になる。
    """
    pending: list[_PendingCost] = []
    correct_est = runner.estimate_correction(cfg, session)
    if correct_est is not None and not ledger.has_entry(entries, session, correct_est.stage):
        pending.append(
            _PendingCost(
                correct_est,
                {"chars": float(runner.transcript_char_count(cfg, session))},
                cfg.pricing.bedrock_input_usd_per_1k,
            )
        )

    sum_est = runner.estimate_summary(cfg, session, claude=claude)
    if (
        sum_est is not None
        and runner.summarize_ran(cfg, session, claude=claude)
        and not ledger.has_entry(entries, session, sum_est.stage)
    ):
        chars = runner.transcript_char_count(cfg, session)
        price = cfg.pricing.claude_input_usd_per_1k if claude else cfg.pricing.bedrock_input_usd_per_1k
        pending.append(_PendingCost(sum_est, {"chars": float(chars)}, price))
    return pending


def _append_pending_costs(cfg: MeetingConfig, session: str, month: str, pending: list[_PendingCost]) -> float:
    """未追記のコスト項目を台帳へ追記し、今回分の合計 USD を返す。

    各項目の load→cumulative_after→append は不可分でなければ累計が壊れるため、ループ全体を
    プロセス内ロックで直列化する（並行パイプラインの累計取りこぼし防止）。
    """
    per_run = 0.0
    with _LEDGER_LOCK:
        for pc in pending:
            current = ledger.load(cfg.ledger_path)
            cum = ledger.cumulative_after(current, month, pc.estimate.usd)
            entry = runner.build_ledger_entry(
                session, pc.estimate, unit_price_usd=pc.unit_price_usd, cumulative_month_usd=cum, units=pc.units
            )
            ledger.append(cfg.ledger_path, entry)
            per_run += pc.estimate.usd
    return round(per_run, 4)


def run_vtt_pipeline(
    cfg: MeetingConfig,
    vtt_path: Path,
    *,
    claude: bool,
    run: SubprocessRunner,
    emit: Emit = print,
) -> tuple[str, int]:
    """VTTインポート（FR-17）の議事録パイプラインを1回：見積提示→実行→台帳追記→閾値警告。

    Transcribe を経由しないため見積・課金は要約段（Bedrock/Claude）のみ。session_id
    （VTTファイル名由来）と rc のタプルを返す。次段の案内は行わない（呼び出し側の責務）。
    """
    session_id = runner.vtt_session_id(vtt_path)
    month = ledger.month_of(ledger.now_iso())
    month_so_far = ledger.monthly_total(ledger.load(cfg.ledger_path), month)
    emit(f"== VTTインポート・議事録パイプライン: {session_id} ==")
    emit(f"  入力        : {vtt_path}")
    emit(
        f"  経路        : {'Claude 生成（PII 外部送信）' if claude else 'Bedrock 生成'}"
        "（Transcribe 不使用のため課金なし）"
    )
    emit(
        f"  今月累計    : ${month_so_far} （上限 単発 ${cfg.thresholds.per_run_usd} / "
        f"月次 ${cfg.thresholds.monthly_usd}）"
    )

    emit("  パイプライン実行中...")
    result = runner.launch_vtt_pipeline(cfg, vtt_path, claude=claude, runner=run)
    # 異常終了でも発生済みの課金（C2 補正・要約）を成果物の痕跡から突き合わせる（run_minutes_pipeline
    # と同じ理由。台帳必須が自動実行の前提のため、失敗時に記録を落とさない）。
    per_run = _record_safely(emit, lambda: record_summary_cost(cfg, session_id, claude, month))

    if result.returncode != 0:
        emit(f"パイプラインが異常終了しました (exit={result.returncode})")
        if result.stderr:
            emit(result.stderr.strip()[-2000:])
        emit(_PARTIAL_COST_WARNING)
        return session_id, result.returncode

    month_total = ledger.monthly_total(ledger.load(cfg.ledger_path), month)
    for w in ledger.evaluate(per_run, month_total, cfg.thresholds):
        emit(f"⚠ コスト警告: {w}")
    return session_id, 0


def run_mp4_pipeline(
    cfg: MeetingConfig,
    mp4_path: Path,
    *,
    claude: bool,
    run: SubprocessRunner,
    emit: Emit = print,
) -> tuple[str, int]:
    """録画 mp4 の取込（FR-18）→ そのまま VTT 議事録パイプライン（FR-17）へ接続する。

    課金は 2 段（mp4→VTT の Transcribe と、後段の要約）。各段の概算を同じ台帳へ追記する。
    session_id（mp4 のファイル名由来。生成 VTT と一致）と rc のタプルを返す。
    """
    session_id = runner.vtt_session_id(mp4_path)
    out_vtt = runner.mp4_vtt_output_path(mp4_path)

    # 変換済み VTT があれば Transcribe を再実行しない（＝再課金しない）。命名ゲートで停止した
    # mp4 セッションの再開は「同じファイルを再指定」＝mp4 の再指定になるため、ここで受け止めないと
    # 満額を再課金した上、既存 VTT（Teams 由来の実名入りを含む）も壊す（mp4_to_vtt 側でも拒否する）。
    if out_vtt.exists():
        emit(f"== mp4 取込・議事録パイプライン: {session_id} ==")
        emit(f"  入力        : {mp4_path}")
        emit(f"  変換済み VTT を再利用します（Transcribe は再実行せず課金なし）: {out_vtt}")
        return run_vtt_pipeline(cfg, out_vtt, claude=claude, run=run, emit=emit)

    month = ledger.month_of(ledger.now_iso())
    month_so_far = ledger.monthly_total(ledger.load(cfg.ledger_path), month)

    # 課金段（Transcribe）の前に尺＝見積を確定させる。取れないなら課金段へ入らず中止する。
    duration_sec = runner.probe_duration_sec(mp4_path, runner=run)
    if duration_sec is None:
        emit(f"エラー: mp4 の長さを取得できませんでした: {mp4_path}")
        emit("  ffprobe（ffmpeg 同梱）が PATH 上にあるか、音声付きの mp4 かを確認してください")
        emit("  （例: `scoop install ffmpeg`）。課金額を見積れないため中止しました。")
        return session_id, 1

    # mp4 は 1 系統のみのため others 側だけに尺を与える（paired の 2 系統合算とは異なる）。
    tr_est = cfg.pricing.estimate_transcribe(0.0, duration_sec)
    emit(f"== mp4 取込・議事録パイプライン: {session_id} ==")
    emit(f"  入力        : {mp4_path}")
    emit(f"  出力VTT     : {out_vtt}")
    emit(f"  経路        : {'Claude 生成（PII 外部送信）' if claude else 'Bedrock 生成'}")
    emit(f"  今回見積    : {tr_est.detail} = ${tr_est.usd}（要約段は VTT 取込後に別途）")
    emit(
        f"  今月累計    : ${month_so_far} （上限 単発 ${cfg.thresholds.per_run_usd} / "
        f"月次 ${cfg.thresholds.monthly_usd}）"
    )

    emit("  mp4→VTT 変換中（ffmpeg で音声抽出 → Transcribe。AWS 課金が発生します）...")
    result = runner.launch_mp4_to_vtt(cfg, mp4_path, out_vtt, runner=run)
    # VTT が生成された＝Transcribe が完走した＝課金済み。異常終了でも記録する（ffmpeg 失敗などで
    # 課金前に落ちた場合は VTT が無いため、幻の計上にはならない）。
    per_run = _record_safely(
        emit, lambda: _record_mp4_transcribe_cost(cfg, session_id, tr_est, duration_sec, month, out_vtt)
    )

    if result.returncode != 0:
        emit(f"mp4→VTT 変換が異常終了しました (exit={result.returncode})")
        if result.stderr:
            emit(result.stderr.strip()[-2000:])
        emit(_PARTIAL_COST_WARNING)
        return session_id, result.returncode

    month_total = ledger.monthly_total(ledger.load(cfg.ledger_path), month)
    for w in ledger.evaluate(per_run, month_total, cfg.thresholds):
        emit(f"⚠ コスト警告: {w}")
    emit(f"  VTT を生成しました: {out_vtt}")

    # 生成した VTT をそのまま議事録経路へ渡す（命名ゲートで停止する。BR-NAME-01）。
    return run_vtt_pipeline(cfg, out_vtt, claude=claude, run=run, emit=emit)


def _record_mp4_transcribe_cost(
    cfg: MeetingConfig, session: str, estimate: Estimate, duration_sec: float, month: str, out_vtt: Path
) -> float:
    """mp4 取込の Transcribe 概算を台帳へ追記し、今回分の合計 USD を返す。

    計上の根拠は出力 VTT の存在（＝Transcribe が完走し課金された痕跡）。変換前に落ちた実行で
    幻の金額を積まないため、痕跡が無ければ 0 を返す。再実行の二重計上は has_entry で防ぐ
    （そもそも VTT があれば呼び出し側が変換を起動しない＝再課金しない）。
    """
    if not out_vtt.exists():
        return 0.0
    entries = ledger.load(cfg.ledger_path)
    if ledger.has_entry(entries, session, estimate.stage):
        return 0.0
    units = {"minutes": round(duration_sec / 60.0, 2)}
    pending = [_PendingCost(estimate, units, cfg.pricing.transcribe_usd_per_minute)]
    return _append_pending_costs(cfg, session, month, pending)


def next_steps_lines(stage: Stage, session: str, claude: bool, *, vtt: bool = False) -> list[str]:
    """次段の案内文を組み立てる（純粋）。CLI/TUI 共通の文言。

    `vtt=True`（VTTインポート由来。FR-17）は録音マニフェストを持たないため、再実行コマンドが
    `meeting minutes` ではなく VTTインポートの再実行になる（`--mode vtt` は既存の
    speaker_names.json/final_transcript.* を検知して続きから進む）。
    """
    resume_hint = "VTTインポートボタンを同じファイルで再実行" if vtt else f"`meeting minutes {session}` を再実行"
    if stage is Stage.NAMING_REQUIRED:
        return [
            "→ 話者名の記入が必要です（BR-NAME-01）。",
            f"  1. Claude に data/out/{session}/final_transcript.merged.json から実名候補を出してもらう",
            f"  2. data/out/{session}/speaker_names.json を記入後、{resume_hint}",
        ]
    if stage is Stage.TRANSCRIPT_DONE:
        if claude:
            return [
                "→ Claude 生成経路で final_transcript.json まで完了（SUMMARIZE_SKIPPED）。",
                f"  Claude に data/out/{session}/final_transcript.json を読ませて minutes.md を生成させる。",
            ]
        return [f"→ トランスクリプトは完了しましたが minutes.md が未生成です。{resume_hint}してください。"]
    if stage is Stage.MINUTES_DONE:
        return [
            f"→ 議事録が完成しました: data/out/{session}/minutes.md",
            f"  Slack へ投稿: `meeting slack {session}`（要約=親 / 全文=スレッド。投稿前にプレビュー＋y/N 承認）。",
            f"  または `/subtext-slack {session}`（Claude が文面提示→人間承認→投稿）。",
        ]
    return []
