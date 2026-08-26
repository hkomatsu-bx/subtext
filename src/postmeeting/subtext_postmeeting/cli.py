"""CLI エントリ（BR-PIPE / BR-ERR）。

argparse で paired/single モード・段指定再実行・S3保持を受け取り、PostMeetingPipeline を駆動する。
失敗時は非ゼロ終了し、どの段で失敗したかを明示する（BR-ERR-03）。ログに音声内容・実名(PII)・認証
情報を出さない（BR-ERR-04, NFR-SEC-04）。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import auth_check, auth_policy, aws
from .config import PipelineConfig
from .errors import PipelineError
from .input_resolver import resolve_paired, resolve_single
from .models import RecordingInput, Stage
from .pipeline import PipelineOptions, PipelineResult, PostMeetingPipeline, ResultStatus
from .vtt_parser import parse_vtt

_STAGE_CHOICES = {s.value: s for s in Stage}


def _resolve_stage_flags(*, no_summarize: bool, no_correct: bool) -> tuple[bool, bool]:
    """CLI フラグから (summarize, correct) を決める（FR-17 / C1 是正）。

    ポリシー: `--no-summarize` は要約段（Bedrock）だけでなく C2 用語補正段も自動で
    スキップする。補正は Bedrock 要約のための前処理であり、要約を踏まない経路（議事録を
    Claude Code 等で外部生成する経路）では不要で、かつ補正段の Bedrock 課金・
    `BEDROCK_MODEL_ID` 依存を避けられる。これにより VTT 経路の `--no-summarize` は
    AWS 課金ゼロ・完全ローカルになる。`--no-correct` 単独は従来どおり補正だけを外す。
    """
    summarize = not no_summarize
    correct = (not no_correct) and summarize
    return summarize, correct


def build_parser() -> argparse.ArgumentParser:
    """CLI 引数パーサを組み立てる（モード paired/single/vtt・段指定・スキップ系フラグ）。"""
    parser = argparse.ArgumentParser(
        prog="subtext-postmeeting",
        description="Subtext Unit B: 会議後パイプライン（文字起こし→話者分離→統合→議事録）。",
    )
    parser.add_argument(
        "--mode",
        choices=["paired", "single", "vtt"],
        help="paired=Unit A の sessionDir / single=単一WAV検証 / vtt=WebVTT（Teams 等）入力（--check-auth 以外では必須）",
    )
    parser.add_argument("--session", type=Path, help="paired: 録音セッションディレクトリ（manifest.json を含む）")
    parser.add_argument("--wav", type=Path, help="single: 検証対象 WAV ファイル")
    parser.add_argument(
        "--vtt",
        type=Path,
        help="vtt: 入力 WebVTT ファイル（Teams 等の文字起こし。Transcribe/S3 を使わない）",
    )
    parser.add_argument("--force", action="store_true", help="全段を再実行（Transcribe 再課金に注意）")
    parser.add_argument(
        "--stage",
        choices=list(_STAGE_CHOICES),
        help="指定段のみ再実行（例: summarized=議事録のみ再生成。Transcribe は再実行されない）",
    )
    parser.add_argument(
        "--no-summarize",
        action="store_true",
        help=(
            "要約段と C2 用語補正段(いずれも Bedrock)を両方スキップし "
            "final_transcript.json まで生成する（議事録は外部生成・AWS 課金なし）"
        ),
    )
    parser.add_argument(
        "--no-correct",
        action="store_true",
        help="LLM 後処理補正(Bedrock)をスキップし原文のまま final_transcript.json を生成する（Bedrock 未課金）",
    )
    parser.add_argument(
        "--no-materials",
        action="store_true",
        help="materials/ の付帯資料を無視して議事録を生成する（③付帯資料）",
    )
    parser.add_argument("--keep-s3", action="store_true", help="成功後も S3 オブジェクトを保持する")
    parser.add_argument("--env-file", type=Path, help=".env のパス（既定: カレントの .env）")
    parser.add_argument(
        "--profile",
        default=None,
        help="使う AWS プロファイル（省略時は環境変数 AWS_PROFILE → .env → SDK 既定）",
    )
    parser.add_argument(
        "--check-auth",
        action="store_true",
        help="AWS 資格情報の疎通確認だけを行う（課金なし・パイプラインは実行しない）",
    )
    parser.add_argument("--verbose", action="store_true", help="詳細ログを出力する")
    return parser


def _reject_conflicting_flags(args: argparse.Namespace, target_stage: Stage | None) -> None:
    """段指定と要約/補正スキップの矛盾を課金前に弾く（BR-ERR-03）。"""
    # --no-summarize と --stage summarized は矛盾（要約段を踏まない/だけ踏む）。
    if args.no_summarize and target_stage is Stage.SUMMARIZED:
        raise PipelineError("--no-summarize と --stage summarized は同時指定できません。", failed_stage="input")
    # --no-correct/--no-summarize と --stage corrected は矛盾（補正段を踏まない/だけ踏む）。
    # --no-summarize は補正段も自動スキップするため（_resolve_stage_flags 参照）。
    if (args.no_correct or args.no_summarize) and target_stage is Stage.CORRECTED:
        raise PipelineError(
            "--no-correct/--no-summarize と --stage corrected は同時指定できません。",
            failed_stage="input",
        )


def main(argv: list[str] | None = None) -> int:
    """エントリポイント。引数解釈→入力解決→パイプライン実行を行い終了コードを返す（0=成功 / 1=失敗）。"""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger("subtext.postmeeting")

    try:
        # 矛盾する組み合わせは、認証確認だけの実行でも先に弾く（`--check-auth` を付けたときだけ
        # 通ってしまうと、次の課金実行で初めて気づくことになる）。
        target_stage = _STAGE_CHOICES[args.stage] if args.stage else None
        _reject_conflicting_flags(args, target_stage)
        if args.check_auth:
            return _run_check_auth(args)
        if args.mode is None:
            # --check-auth 以外では必須。argparse の required=True を外したため自前で弾く。
            raise PipelineError("--mode は必須です（paired / single / vtt）。", failed_stage="input")
        # VTT 経路は Transcribe/S3 を使わないため S3_BUCKET 必須化を外す。
        config = PipelineConfig.from_env(args.env_file, require_s3=(args.mode != "vtt"))
        aws.apply_profile(aws.resolve_profile(args.profile, config.aws_profile))
        if args.mode == "vtt":
            result = _run_vtt(args, config, target_stage)
        else:
            rec = _resolve_input(args, config.language)
            summarize, correct = _resolve_stage_flags(no_summarize=args.no_summarize, no_correct=args.no_correct)
            options = PipelineOptions(
                input=rec,
                force=args.force,
                target_stage=target_stage,
                keep_s3=args.keep_s3 or config.keep_s3,
                summarize=summarize,
                correct=correct,
                materials=not args.no_materials,
            )
            result = PostMeetingPipeline(config).run(options)
    except PipelineError as exc:
        # 失敗段を明示し非ゼロ終了（BR-ERR-03）。例外メッセージに PII を載せない前提（BR-ERR-04）。
        logger.error("パイプライン失敗 %s", exc)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        logger.error("中断されました。")
        return 1

    if result.status in (ResultStatus.NAMING_REQUIRED, ResultStatus.SUMMARIZE_SKIPPED):
        logger.info(result.message)
        print(result.message)
        return 0

    print(f"完了: {result.minutes_path}")
    print(f"最終トランスクリプト: {result.final_transcript_path}")
    return 0


def _run_check_auth(args: argparse.Namespace) -> int:
    """資格情報の疎通確認だけを行う（課金なし。FR-H2-07 / FR-H2-11・BR-H2-AUTH-02）。

    段 0 の事前チェックは「課金段の直前」に走るため、失効は議事録生成を始めてから分かる。
    会議ハーネスは録音前にこれを叩いて操作者へ知らせるので、単体で呼べる経路を用意する。

    結果は機械可読な 1 行として stdout へ出す（Unit A の `sessionId=` と同方針）。
    出すのは成否・profile・region に限り、アカウント ID・ARN は出さない（BR-H2-AUTH-04・
    NFR-SEC-04）。呼び出し側がこの出力を画面とログへ流すためである。

    S3 バケットの必須検証は外す（認証確認に S3 は要らない）。
    """
    config = PipelineConfig.from_env(args.env_file, require_s3=False)
    profile = aws.resolve_profile(args.profile, config.aws_profile)
    aws.apply_profile(profile)
    # 解決済みの名前を渡す（存在しないプロファイル名でも「default で失敗」と報告しないため）。
    probe = auth_check.probe_credentials(config.aws_region, profile=profile)
    print(f"authProfile={probe.profile}")
    print(f"authRegion={config.aws_region}")
    print(f"authStatus={probe.status}")
    if probe.kind is None:  # ok（`probe.ok` と同義。型を絞るためこちらで書く）
        return 0
    print(auth_policy.remediation_message(probe.kind))
    return 1


def _run_vtt(args: argparse.Namespace, config: PipelineConfig, target_stage: Stage | None) -> PipelineResult:
    """vtt モード: WebVTT を FinalTranscript に変換し、命名→議事録のみを駆動する。"""
    if args.vtt is None:
        raise PipelineError("--mode vtt には --vtt が必須です。", failed_stage="input")
    if not args.vtt.is_file():
        raise PipelineError(f"VTT が見つかりません: {args.vtt}", failed_stage="input")
    try:
        text = args.vtt.read_text(encoding="utf-8")
    except OSError as exc:
        raise PipelineError(f"VTT を読み込めません: {args.vtt}", failed_stage="input") from exc
    # セッション ID は VTT ファイル名（拡張子なし）由来（single モードと同方針）。
    merged = parse_vtt(text, args.vtt.stem, config.language)
    summarize, correct = _resolve_stage_flags(no_summarize=args.no_summarize, no_correct=args.no_correct)
    return PostMeetingPipeline(config).run_from_transcript(
        merged,
        summarize=summarize,
        correct=correct,
        materials=not args.no_materials,
        force=args.force,
        target_stage=target_stage,
    )


def _resolve_input(args: argparse.Namespace, language: str) -> RecordingInput:
    """paired（--session）/ single（--wav）の必須引数を検証し RecordingInput を解決する。"""
    if args.mode == "paired":
        if args.session is None:
            raise PipelineError("--mode paired には --session が必須です。", failed_stage="input")
        return resolve_paired(args.session, language)
    if args.wav is None:
        raise PipelineError("--mode single には --wav が必須です。", failed_stage="input")
    return resolve_single(args.wav, language)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
