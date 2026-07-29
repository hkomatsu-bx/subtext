"""mp4→VTT 変換ツール（独立 CLI: subtext-mp4-to-vtt, FR-18）。

会議サービス（Teams 等）の録画 mp4 から ffmpeg で音声を抽出（16kHz/mono FLAC）し、Amazon
Transcribe（話者分離）にかけて WebVTT を生成する。Teams の動画 mp4 は Transcribe が直接 parse
できない（"Failed to parse audio file"）ため、音声抽出を挟む。出力 VTT は Teams VTT と同じ
`<v spk_0>` 書式なので、そのまま `subtext-postmeeting --mode vtt --vtt <出力>` に渡して議事録化
できる（実名は後段の命名ゲートで付与）。

Unit B 本体の S3/Transcribe/parse を再利用し、VTT 出力のみ新規（vtt_writer）。前提: ffmpeg が
PATH 上にあること。AWS 課金は Transcribe のみ。成功・失敗いずれの経路でも当該セッションの S3 と
抽出音声（一時ファイル）を後始末する（keep_s3 で S3 は保持）。PII（音声内容・実名）はログに
出さない（BR-ERR-04, NFR-SEC-04）。
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

from . import parser, vtt_writer
from .config import PipelineConfig
from .errors import PipelineError
from .models import StreamRole
from .pipeline import make_credential_checker
from .s3_io import S3Io, safe_key_segment
from .transcribe_client import TranscribeClient, make_job_spec

logger = logging.getLogger("subtext.mp4_to_vtt")

_STAGE = "mp4-to-vtt"
# Teams 等の動画 mp4 は Transcribe の直接 parse に失敗する（"Failed to parse audio file"）。
# ffmpeg で音声のみを可逆抽出（16kHz/mono FLAC）してから Transcribe にかける。
_MEDIA_FORMAT = "flac"
_FFMPEG = "ffmpeg"
_SAMPLE_RATE_HZ = 16_000  # 音声認識に十分・Transcribe 推奨レンジ（8k〜48k）内。


def convert(
    mp4_path: Path,
    out_path: Path,
    config: PipelineConfig,
    *,
    keep_s3: bool = False,
    force: bool = False,
    s3io: S3Io | None = None,
    transcribe: TranscribeClient | None = None,
    credential_checker: Callable[[], None] | None = None,
    audio_extractor: Callable[[Path, Path], None] | None = None,
) -> Path:
    """mp4 から音声を抽出 → Transcribe（話者分離）→ WebVTT を out_path に書く。

    AWS 依存（S3/Transcribe/資格情報チェッカ）と音声抽出は注入可能（テストは fake を渡す）。
    抽出音声（機微）は一時ディレクトリに置き、終了時に必ず削除する。
    既存の出力 VTT は上書きしない（`force=True` で明示的に上書きする）。
    """
    if not mp4_path.is_file():
        raise PipelineError(f"mp4 が見つかりません: {mp4_path}", failed_stage=_STAGE)
    # 出力先の既存 VTT を黙って壊さない（BR-IO-01 と同旨。録音側 SyncRecorder の上書き禁止に倣う）。
    # 既定の出力先は入力 mp4 と同じ場所の `.vtt` で、Teams は録画 mp4 と実名入り VTT を同じ
    # フォルダへ出す。上書きすると実名入りトランスクリプトが失われる上、課金段（Transcribe）へ
    # 入ってしまうため、課金前にここで止める。
    if out_path.exists() and not force:
        raise PipelineError(
            f"出力 VTT が既に存在します: {out_path}\n"
            "上書きすると元の VTT（Teams が出力した実名入りトランスクリプト等）が失われ、"
            "Transcribe も再課金されます。既存 VTT をそのまま `--mode vtt` に渡すか、"
            "`--out` で別の出力先を指定するか、意図した上書きなら `--force` を付けてください。",
            failed_stage=_STAGE,
        )

    extract = audio_extractor if audio_extractor is not None else extract_audio
    safe_id = _safe_id(mp4_path.stem)
    prefix = config.s3_prefix
    media_key = f"{prefix}{safe_id}/audio.flac"
    output_key = f"{prefix}{safe_id}/transcribe/others.json"

    s3 = s3io if s3io is not None else S3Io(config.s3_bucket, config.aws_region)
    check = credential_checker if credential_checker is not None else make_credential_checker(config.aws_region)

    tmp_dir = Path(tempfile.mkdtemp(prefix="subtext-mp4-"))
    audio_path = tmp_dir / f"{safe_id}.flac"
    try:
        # 課金段の前に資格情報を確認（無効なら actionable 停止, BR-H2-AUTH-02）。
        check()
        # mp4 → 16kHz/mono FLAC（ffmpeg）。Transcribe は動画 mp4 を直接 parse できないため。
        extract(mp4_path, audio_path)
        s3.verify_preconditions()  # BR-IO-01（暗号化・公開ブロック前提）
        media_uri = s3.upload(audio_path, media_key)

        spec = make_job_spec(
            safe_id,
            StreamRole.OTHERS,  # 話者分離を有効化（ShowSpeakerLabels=true）。
            media_uri,
            config.language,
            config.max_speakers,
            config.vocabulary_name,
            media_format=_MEDIA_FORMAT,
        )
        tc = transcribe if transcribe is not None else TranscribeClient(config.aws_region)
        tc.run_job(spec, s3.bucket, output_key, config.poll_timeout_sec)

        raw_json = s3.download_json(output_key)
        raw_transcript = parser.parse(raw_json, StreamRole.OTHERS, mp4_path.stem, config.language)
        vtt = vtt_writer.to_vtt(raw_transcript)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(vtt, encoding="utf-8")
        return out_path
    finally:
        _cleanup(s3, f"{prefix}{safe_id}/", keep_s3)
        shutil.rmtree(tmp_dir, ignore_errors=True)  # 抽出音声(機微)を必ず破棄。


def extract_audio(mp4_path: Path, out_path: Path) -> None:
    """ffmpeg で mp4 から 16kHz/mono FLAC を抽出する（動画ストリームは破棄）。

    ffmpeg 未導入や抽出失敗は actionable な PipelineError で停止する。stderr は PII（パス＝顧客名
    を含みうる）保護のためメッセージに載せず、詳細は DEBUG ログに留める（BR-ERR-04）。
    """
    cmd = [
        _FFMPEG,
        "-nostdin",
        "-y",
        "-i",
        str(mp4_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(_SAMPLE_RATE_HZ),
        "-c:a",
        "flac",
        str(out_path),
    ]
    try:
        # stderr はバイトで受ける。text=True だと Windows 既定(cp932)でデコードして
        # ffmpeg 出力の非 cp932 バイトで落ちるため、デコードは errors="replace" で後段に回す。
        proc = subprocess.run(cmd, capture_output=True)
    except FileNotFoundError as exc:
        raise PipelineError(
            "ffmpeg が見つかりません。導入してください（例: `scoop install ffmpeg`）。",
            failed_stage=_STAGE,
        ) from exc
    stderr_text = proc.stderr.decode("utf-8", "replace") if proc.stderr else ""
    if proc.returncode != 0:
        logger.debug("ffmpeg stderr: %s", stderr_text)
        # 映像のみ（音声トラック無し）の mp4 は -vn 後にストリームが消え、この signature で落ちる。
        if "does not contain any stream" in stderr_text:
            raise PipelineError(
                "この mp4 には音声トラックがありません（映像のみ）。Teams が出力する VTT を "
                "`--mode vtt` で直接使うか、音声付きの録画を入力してください。",
                failed_stage=_STAGE,
            )
        raise PipelineError(
            f"ffmpeg による音声抽出に失敗しました（returncode={proc.returncode}）。"
            "--verbose で詳細ログを確認してください。",
            failed_stage=_STAGE,
        )
    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise PipelineError(
            "音声抽出の結果が空です（mp4 に音声トラックが無い可能性があります）。",
            failed_stage=_STAGE,
        )


def _cleanup(s3: S3Io, prefix: str, keep_s3: bool) -> None:
    """当該セッションの S3 を後始末する（フェイルセーフ・keep_s3 で保持, BR-H2-S3-01）。"""
    if keep_s3:
        return
    try:
        deleted = s3.delete_prefix(prefix)
        if deleted:
            logger.info("S3 後始末: %d 個のオブジェクトを削除（prefix=%s）", deleted, prefix)
    except Exception as exc:  # noqa: BLE001 フェイルセーフ（成果物を毀損しない）
        logger.warning("S3 後始末に失敗しました（処理は継続）: %s", exc)


def _safe_id(stem: str) -> str:
    """S3 キー/Transcribe ジョブ名に安全な ASCII ID へ変換する（規則は s3_io に集約）。"""
    return safe_key_segment(stem)


def build_parser() -> argparse.ArgumentParser:
    parser_ = argparse.ArgumentParser(
        prog="subtext-mp4-to-vtt",
        description="会議録画 mp4 から音声抽出(ffmpeg)→Transcribe(話者分離)で WebVTT 化する独立ツール。",
    )
    parser_.add_argument("mp4", type=Path, help="入力 mp4（Teams 等の録画）")
    parser_.add_argument("--out", type=Path, help="出力 VTT パス（既定: 入力と同じ場所に .vtt）")
    parser_.add_argument("--keep-s3", action="store_true", help="成功後も S3 オブジェクトを保持する")
    parser_.add_argument(
        "--force",
        action="store_true",
        help="出力 VTT が既にあっても上書きする（既存 VTT を失い Transcribe を再課金する点に注意）",
    )
    parser_.add_argument("--env-file", type=Path, help=".env のパス（既定: カレントの .env）")
    parser_.add_argument("--verbose", action="store_true", help="詳細ログを出力する")
    return parser_


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = PipelineConfig.from_env(args.env_file)  # S3/Transcribe 必須＝require_s3=True
        out_path = args.out if args.out is not None else args.mp4.with_suffix(".vtt")
        result = convert(
            args.mp4, out_path, config, keep_s3=args.keep_s3 or config.keep_s3, force=args.force
        )
    except PipelineError as exc:
        logger.error("mp4→VTT 変換失敗 %s", exc)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        logger.error("中断されました。")
        return 1

    print(f"VTT 生成: {result}")
    print(f'→ 議事録化: subtext-postmeeting --mode vtt --vtt "{result}"')
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
