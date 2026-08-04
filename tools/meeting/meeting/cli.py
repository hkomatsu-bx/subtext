"""会議ハーネスの CLI（record / stop / minutes / process / slack / status / cost）。

ターミナル起点で録音(Unit A)・議事録(Unit B)を駆動する。課金・PII は「見積→自動実行→
台帳追記→閾値警告」の順に制御する。Slack 投稿は外部公開のため、投稿前にプレビューと y/N
確認を必ず挟む（人間承認を残す）。Bot Token は秘密として扱いログに出さない（BR-SEC-01）。

ユーザー向け出力は print、診断ログは logging（標準エラー）に分ける。
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime
from typing import Callable, Sequence

from meeting import ledger, pipeline, runner, slack, wizard
from meeting.config import MeetingConfig, load_config
from meeting.runner import Runner, Stage

logger = logging.getLogger("meeting")


def main(
    argv: Sequence[str] | None = None,
    *,
    cfg: MeetingConfig | None = None,
    run: Runner = subprocess.run,
    ask: Callable[[str], str] = input,
    post: slack.Poster | None = None,
) -> int:
    """エントリポイント。cfg/run/ask/post はテスト用 seam。

    ask は process/slack の対話入力、post は Slack HTTP トランスポート（None で実 urllib）。
    """
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    _configure_utf8_output()
    args = _build_parser().parse_args(argv)
    if not getattr(args, "command", None):
        _build_parser().print_help()
        return 2

    # 対話入力・Slack トランスポートの seam を handler へ渡す（使うのは process/slack のみ）。
    args.ask = ask
    args.post = post
    try:
        # 設定読込も同じハンドラの内側に入れる。load_config が投げるのは、まさに下の except が
        # 1行の actionable エラーへ畳むために列挙している型そのもの（リポジトリルート不明や
        # meeting.toml 不在の FileNotFoundError、必須キー欠落の KeyError、chars_per_token<=0 の
        # ValueError）。外に出していたため生トレースバックが出ていた。
        config = cfg or load_config()
        # `--profile` は環境変数・.env より優先する（AWS を触るサブコマンドにのみ存在）。
        if profile := (getattr(args, "profile", None) or "").strip():
            config = replace(config, aws_profile=profile)
        rc: int = args.handler(config, args, run)
    except (FileNotFoundError, ValueError, KeyError) as exc:
        # 想定内の失敗（ファイル欠落・壊れた manifest/台帳 JSON・必須キー欠落）は
        # 生トレースバックでなく1行の actionable エラーで非ゼロ終了する。
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    return rc


def _configure_utf8_output() -> None:
    """stdout/stderr を UTF-8 に寄せる（Windows の既定 cp932 対策）。

    Slack mrkdwn のプレビューには `•` など cp932 で表現できない文字が含まれ、cp932 コンソールへ
    print すると UnicodeEncodeError で落ちる。これを防ぐ。差し替え不可のストリーム（テストの
    capsys 等）では黙ってスキップする。
    """
    for stream_name, stream in (("stdout", sys.stdout), ("stderr", sys.stderr)):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue  # 差し替え不可のストリーム（capsys 等）はスキップ（ガード節）。
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError) as exc:
            # 無言破棄はしない（この関数の目的＝cp932 由来の UnicodeEncodeError 防止が
            # 果たせない可能性を残すため）。診断のみで処理は継続する。
            logger.warning("%s を UTF-8 に再設定できませんでした（cp932 のままの可能性）: %s", stream_name, exc)


def _add_profile_option(sub_parser: argparse.ArgumentParser) -> None:
    """AWS を触るサブコマンドに `--profile` を足す。

    付けるのは AWS を呼ぶ経路だけ（`record`／`stop`／`status`／`cost`／`slack` は AWS を
    使わない）。解決した値は子プロセスへ `AWS_PROFILE` として注入する。
    """
    sub_parser.add_argument(
        "--profile",
        default=None,
        help="使う AWS プロファイル（省略時は環境変数 AWS_PROFILE → ルート .env → SDK 既定）",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meeting", description="Subtext 会議ハーネス")
    sub = parser.add_subparsers(dest="command")

    p_record = sub.add_parser("record", help="録音を開始（別ターミナルで実行）")
    p_record.add_argument(
        "minutes",
        nargs="?",
        type=int,
        default=runner.DEFAULT_RECORD_MINUTES,
        help=f"最大録音分（既定 {runner.DEFAULT_RECORD_MINUTES}）",
    )
    p_record.set_defaults(handler=_cmd_record)

    p_stop = sub.add_parser("stop", help="停止ファイルを作成して録音を止める")
    p_stop.set_defaults(handler=_cmd_stop)

    p_live = sub.add_parser("live", help="ライブ字幕を開始（確定字幕を JSONL 永続化。別ターミナルで実行）")
    _add_profile_option(p_live)
    p_live.set_defaults(handler=_cmd_live)

    p_minutes = sub.add_parser("minutes", help="議事録パイプラインを見積→実行→台帳記録")
    p_minutes.add_argument("session", nargs="?", default=None, help="対象セッション（既定=最新）")
    p_minutes.add_argument(
        "--claude", action="store_true", help="Bedrock 要約をスキップ（Claude 生成経路。実名 PII を外部送信）"
    )
    _add_profile_option(p_minutes)
    p_minutes.set_defaults(handler=_cmd_minutes)

    p_process = sub.add_parser("process", help="録音済→議事録を対話1コマンドで駆動（後処理ウィザード）")
    p_process.add_argument("session", nargs="?", default=None, help="対象セッション（既定=最新）")
    p_process.add_argument(
        "--claude", action="store_true", help="Bedrock 要約をスキップ（Claude 生成経路。実名 PII を外部送信）"
    )
    p_process.add_argument("--channel", default=None, help="Slack 投稿先チャンネルID（省略時は既定 or 対話入力）")
    _add_profile_option(p_process)
    p_process.set_defaults(handler=_cmd_process)

    p_slack = sub.add_parser(
        "slack", help="生成済み議事録を Slack へ投稿（要約=親 / 全文=スレッド。要 SLACK_BOT_TOKEN）"
    )
    p_slack.add_argument("session", nargs="?", default=None, help="対象セッション（既定=最新）")
    p_slack.add_argument("--channel", default=None, help="投稿先チャンネルID（省略時は既定 or 対話入力）")
    p_slack.set_defaults(handler=_cmd_slack)

    p_status = sub.add_parser("status", help="セッションの現在段を表示")
    p_status.add_argument("session", nargs="?", default=None, help="対象セッション（既定=最新）")
    p_status.set_defaults(handler=_cmd_status)

    p_cost = sub.add_parser("cost", help="コスト台帳の月次サマリを表示")
    p_cost.add_argument("--month", default=None, help="対象月 YYYY-MM（既定=今月）")
    p_cost.set_defaults(handler=_cmd_cost)

    p_tui = sub.add_parser("tui", help="画面操作(TUI)でセッション一覧・停止・議事録・Slack投稿を行う")
    _add_profile_option(p_tui)
    p_tui.set_defaults(handler=_cmd_tui)

    return parser


# --- record / stop ------------------------------------------------------------


def _cmd_record(cfg: MeetingConfig, args: argparse.Namespace, run: Runner) -> int:
    minutes = args.minutes
    if runner.needs_recorder_build(cfg):
        print("録音 exe が未ビルド/古いためビルドします（dotnet build）...")
        build = run(["dotnet", "build", "src/recorder", "-c", "Release"], cwd=str(cfg.repo_root))
        if build.returncode != 0:
            print("ビルドに失敗しました。", file=sys.stderr)
            return build.returncode
    exe = runner.resolve_recorder_exe(cfg)
    if exe is None:
        print(
            "録音 exe を解決できませんでした（dist/recorder も src/recorder/bin も見つかりません）。", file=sys.stderr
        )
        return 1
    cmd = runner.build_recorder_command(cfg, exe, minutes)
    print(f"録音開始（最大 {minutes} 分 / Ctrl+C か `meeting stop` で停止）")
    print("注意: 実機 WASAPI を掴むため、必ず通常のターミナルで実行すること。")
    result = run(cmd, cwd=str(cfg.repo_root))
    return result.returncode


def _cmd_stop(cfg: MeetingConfig, args: argparse.Namespace, run: Runner) -> int:
    cfg.stop_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.stop_file.touch()
    print(f"停止ファイルを作成しました: {cfg.stop_file}")
    print("録音側に `Saved to recordings/<session> (status=complete)` が出れば停止完了。")
    return 0


# --- live（ライブ字幕・B1F） ---------------------------------------------------


def _cmd_live(cfg: MeetingConfig, args: argparse.Namespace, run: Runner) -> int:
    if runner.needs_live_build(cfg):
        print("ライブ字幕 exe が未ビルド/古いためビルドします（dotnet build）...")
        build = run(["dotnet", "build", "src/live", "-c", "Release"], cwd=str(cfg.repo_root))
        if build.returncode != 0:
            print("ビルドに失敗しました。", file=sys.stderr)
            return build.returncode
    exe = runner.resolve_live_exe(cfg)
    if exe is None:
        print("ライブ字幕 exe を解決できませんでした（dist/live も src/live/bin も見つかりません）。", file=sys.stderr)
        return 1

    session = runner.new_session_id(datetime.now())
    out_dir = cfg.session_out_dir(session)
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl = cfg.live_captions_path(session)
    stop_file = cfg.live_stop_file(session)
    if stop_file.exists():
        stop_file.unlink()  # 前回の残骸を除去（起動直後の誤停止を防ぐ）

    print(f"== ライブ字幕開始: セッション {session} ==")
    print(f"  字幕JSONL : {jsonl}")
    print(f"  停止      : Ctrl+C（または別ターミナルで {stop_file} を作成）")
    print("  注意      : 実機 WASAPI/AWS を掴むため、必ず通常のターミナルで実行すること。")

    try:
        result = runner.run_live(cfg, exe, session, runner=run)
        rc = result.returncode
    except KeyboardInterrupt:
        stop_file.parent.mkdir(parents=True, exist_ok=True)
        stop_file.touch()  # 子へ協調停止を通知（子の Ctrl+C ハンドラと二重でも安全）
        print("\n停止シグナルを送信しました。字幕の確定を待っています...")
        rc = 0

    print("== ライブ字幕終了 ==")
    print("会議後の議事録化（任意・AWS 課金は要約段のみ）:")
    print(f"  1. uv run subtext-live-to-vtt {jsonl}")
    print(f"  2. uv run subtext-postmeeting --mode vtt --vtt {jsonl.with_suffix('.vtt')}")
    print("     （課金ゼロで止めるなら末尾に --no-summarize）")
    return rc


# --- minutes ------------------------------------------------------------------


def _cmd_minutes(cfg: MeetingConfig, args: argparse.Namespace, run: Runner) -> int:
    session = runner.resolve_session(cfg, args.session)
    stage = runner.detect_stage(cfg, session)
    if stage is Stage.NO_RECORDING:
        print(f"エラー: セッション {session} に manifest.json がありません。", file=sys.stderr)
        return 1
    if not runner.has_manifest(cfg, session):
        # VTTインポート由来（FR-17）は録音マニフェストを持たず paired 経路では処理できない。
        print(
            f"エラー: セッション {session} はVTTインポート由来です。"
            "`subtext-postmeeting --mode vtt --vtt <同じファイル>` を再実行してください。",
            file=sys.stderr,
        )
        return 1

    rc = pipeline.run_minutes_pipeline(cfg, session, claude=args.claude, run=run)
    if rc != 0:
        return rc

    # 次段の案内。
    _print_next_steps(runner.detect_stage(cfg, session), session, args.claude)
    return 0


def _cmd_process(cfg: MeetingConfig, args: argparse.Namespace, run: Runner) -> int:
    """後処理ウィザード: 録音済→minutes.md を対話1コマンドで駆動する。

    段検知で現在地を示し、話者名ゲートはその場で入力→書き戻し→自動再実行する。各パイプ
    ライン実行は minutes と同じ経路（pipeline.run_minutes_pipeline）を通すため課金・台帳の扱いは同一。
    """
    session = runner.resolve_session(cfg, args.session)
    stage = runner.detect_stage(cfg, session)
    if stage is Stage.NO_RECORDING:
        print(f"エラー: セッション {session} に manifest.json がありません。", file=sys.stderr)
        return 1
    if not runner.has_manifest(cfg, session):
        # VTTインポート由来（FR-17）は録音マニフェストを持たず paired 経路では処理できない。
        print(
            f"エラー: セッション {session} はVTTインポート由来です。"
            "`subtext-postmeeting --mode vtt --vtt <同じファイル>` を再実行してください。",
            file=sys.stderr,
        )
        return 1

    def run_minutes(sess: str) -> int:
        return pipeline.run_minutes_pipeline(cfg, sess, claude=args.claude, run=run)

    def slack_offer(sess: str) -> None:
        _offer_slack_post(cfg, sess, args)

    return wizard.run(
        cfg,
        session,
        claude=args.claude,
        run_minutes=run_minutes,
        ask=args.ask,
        slack_offer=slack_offer,
    )


# --- slack --------------------------------------------------------------------


def _cmd_slack(cfg: MeetingConfig, args: argparse.Namespace, run: Runner) -> int:
    """生成済み議事録を Slack へ投稿する（要約=親 / 全文=スレッド）。

    minutes.md が無ければ停止。トークン欠落・チャンネル未指定も明示エラー。投稿前にプレビュー
    と y/N 確認を挟む（外部公開のため人間承認を残す）。課金は発生しない。
    """
    session = runner.resolve_session(cfg, args.session)
    if runner.detect_stage(cfg, session) is not Stage.MINUTES_DONE:
        print(f"エラー: セッション {session} に minutes.md がありません（先に process/minutes）。", file=sys.stderr)
        return 1

    token = slack.resolve_slack_token(cfg.repo_root, os.environ)
    if not token:
        print("エラー: SLACK_BOT_TOKEN が未設定です（環境変数かルート .env に設定）。", file=sys.stderr)
        return 1

    channel = _resolve_channel(cfg, args)
    if not channel:
        print("エラー: 投稿先チャンネルが指定されていません（--channel か meeting.toml）。", file=sys.stderr)
        return 1

    minutes_md = _read_minutes(cfg, session)
    return _confirm_and_post(session, channel, minutes_md, token=token, ask=args.ask, post=args.post)


def _offer_slack_post(cfg: MeetingConfig, session: str, args: argparse.Namespace) -> None:
    """process ウィザードの最終段: 任意で Slack 投稿を提案する（スキップは無害）。"""
    token = slack.resolve_slack_token(cfg.repo_root, os.environ)
    if not token:
        print("（Slack 投稿は SLACK_BOT_TOKEN 未設定のためスキップ。設定すると投稿できます）")
        return
    if args.ask("Slack へ投稿しますか? [y/N] ").strip().lower() not in ("y", "yes"):
        return
    channel = _resolve_channel(cfg, args)
    if not channel:
        print("チャンネル未指定のため投稿をスキップしました。")
        return
    try:
        _confirm_and_post(session, channel, _read_minutes(cfg, session), token=token, ask=args.ask, post=args.post)
    except (ValueError, OSError) as exc:
        # 議事録生成は成功済み。任意オファーの投稿失敗で process 全体を失敗させない（スキップは無害）。
        # 冪等性は未実装のため、再投稿すると親＋全文が重複する点に注意。
        print(f"（Slack 投稿に失敗しました: {exc}）", file=sys.stderr)
        print(
            f"（議事録は生成済みです。`meeting slack {session}` で再投稿できます"
            "（再投稿時は既存メッセージと重複します））",
            file=sys.stderr,
        )


def _resolve_channel(cfg: MeetingConfig, args: argparse.Namespace) -> str:
    """投稿先チャンネルを解決する: --channel → meeting.toml 既定 → 対話入力。"""
    channel = getattr(args, "channel", None) or cfg.slack_default_channel
    if channel:
        return channel.strip()
    answer: str = args.ask("投稿先チャンネルID > ")
    return answer.strip()


def _read_minutes(cfg: MeetingConfig, session: str) -> str:
    path = cfg.session_out_dir(session) / "minutes.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"minutes.md を読み込めません: {path}: {exc}") from exc


def _confirm_and_post(
    session: str,
    channel: str,
    minutes_md: str,
    *,
    token: str,
    ask: Callable[[str], str],
    post: slack.Poster | None,
) -> int:
    """プレビュー→ y/N 確認→投稿。中止は 0（無害）。

    外部公開のため、親メッセージ（要約）だけでなくスレッドへ投稿する全文（mrkdwn 変換後）も
    プレビューに含め、y/N 承認ゲートが投稿内容全体をカバーするようにする。
    """
    parent = slack.build_parent(session, minutes_md)
    # post_minutes と同じ入力（決定事項・ToDo を除いた本文）でプレビューする。
    # ここで minutes_md をそのまま渡すと、実際の投稿内容（重複除去済み）とプレビューがずれる。
    body = slack.to_mrkdwn(slack.build_thread_body(minutes_md))
    print("== Slack 投稿プレビュー ==")
    print(f"  投稿先チャンネル: {channel}")
    print("  親メッセージ（要約）:")
    for line in parent.splitlines():
        print(f"    {line}")
    print("  スレッド全文（分割してスレッドに投稿されます）:")
    for line in body.splitlines():
        print(f"    {line}")
    if ask("この内容で投稿しますか? [y/N] ").strip().lower() not in ("y", "yes"):
        print("投稿を中止しました。")
        return 0
    # y/N 承認を通過した＝人間承認済み。プレビュー生成済みの parent を渡して二重組立を避ける。
    return slack.post_minutes(
        session, minutes_md, channel, token=token, approved=True, poster=post, parent=parent
    )


def _print_next_steps(stage: Stage, session: str, claude: bool) -> None:
    for line in pipeline.next_steps_lines(stage, session, claude):
        print(line)


# --- status / cost ------------------------------------------------------------


def _cmd_status(cfg: MeetingConfig, args: argparse.Namespace, run: Runner) -> int:
    session = runner.resolve_session(cfg, args.session)
    stage = runner.detect_stage(cfg, session)
    print(f"セッション: {session}")
    print(f"  段        : {runner.STAGE_LABELS[stage]}")
    # VTTインポート由来（FR-17）は録音マニフェストを持たないため、存在確認してから読む。
    manifest_path = cfg.session_recording_dir(session) / "manifest.json"
    if manifest_path.is_file():
        manifest = runner.read_manifest(cfg, session)
        print(f"  録音状態  : {manifest.status}")
        print(f"  録音長    : self {manifest.self_sec:.1f}s / others {manifest.others_sec:.1f}s")
    return 0


def _cmd_cost(cfg: MeetingConfig, args: argparse.Namespace, run: Runner) -> int:
    entries = ledger.load(cfg.ledger_path)
    month = ledger.normalize_month(args.month) if args.month else ledger.month_of(ledger.now_iso())
    total = ledger.monthly_total(entries, month)
    month_entries = [e for e in entries if ledger.month_of(e.ts) == month]

    print(f"== コスト概算サマリ ({month}) ==")
    print(f"  月次累計  : ${total} （上限 ${cfg.thresholds.monthly_usd}）")
    print(f"  記録件数  : {len(month_entries)}")
    for e in month_entries[-10:]:
        pii = " [PII送信]" if e.pii_sent else ""
        print(f"  {e.ts}  {e.session}  {e.stage:<10} ${e.est_usd}{pii}")
    if total > cfg.thresholds.monthly_usd:
        print(f"⚠ 月次累計が上限 ${cfg.thresholds.monthly_usd} を超過しています。")
    print("  ※ 概算です。正確な請求額は AWS Cost Explorer を参照。")
    return 0


# --- tui ------------------------------------------------------------------


def _cmd_tui(cfg: MeetingConfig, args: argparse.Namespace, run: Runner) -> int:
    """画面操作(TUI)を起動する。Textual は重いため他サブコマンドの起動を軽く保つべく遅延 import する。

    録音は TUI からも開始・停止できる（`r` キー）。**ライブ字幕の開始のみスコープ外**
    （実機 WASAPI と前景表示の制約のため `meeting live` を使う）。

    停止シグナル（`data/recordings/.stop`）は録音全体で 1 つを共有するため、TUI の停止も
    `meeting stop` も、そのとき動いている recorder すべてに効く。TUI と `meeting record` を
    同時に走らせても WASAPI は共有モードで両方キャプチャでき、セッションIDが秒単位で異なる限り
    出力も衝突しない（同一秒の多重起動は SyncRecorder の上書き禁止＝BR-IO-01 が弾く）。
    ただし同じ会議を二重に録って Transcribe を二重に払うことになるため、避けること。
    """
    from meeting.tui import MeetingApp

    app = MeetingApp(cfg, run=run, post=args.post)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
