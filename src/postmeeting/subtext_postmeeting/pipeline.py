"""会議後パイプライン・オーケストレータ（BR-PIPE）。

L1→L8 を順に駆動し、成果物ファイルの有無で中間成果物を再利用する（Q4=A）。`--force` で全段再実行、
段指定再実行（例: summarize のみ）も可（BR-PIPE-02）。Transcribe 段は明示時または成果物欠落時のみ
実行し、意図せぬ再課金を避ける（BR-PIPE-04）。AWS クライアントは遅延生成（summarize のみ再実行など、
不要な段で認証情報を要求しない）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from . import (
    auth_check,
    auth_policy,
    correction as correction_mod,
    merger,
    parser,
    speaker_label,
    summarize as summarize_mod,
)
from .config import PipelineConfig
from .errors import PipelineError
from .models import (
    FinalTranscript,
    InputMode,
    RawTranscript,
    RecordingInput,
    Stage,
    StreamRole,
)
from .s3_io import S3Io, safe_key_segment
from .transcribe_client import TranscribeClient, make_job_spec

logger = logging.getLogger("subtext.postmeeting")


class ResultStatus(str, Enum):
    COMPLETED = "completed"
    NAMING_REQUIRED = "naming_required"  # 話者マッピング記入待ちで停止（BR-NAME-01）
    # 要約段スキップで final まで。correct=True なら補正段で Bedrock 課金あり（CLI は --no-summarize で補正も自動オフ）
    SUMMARIZE_SKIPPED = "summarize_skipped"


@dataclass(frozen=True)
class PipelineOptions:
    """1回の実行を駆動するパラメータ（CLI 由来）。"""

    input: RecordingInput
    force: bool = False
    target_stage: Stage | None = None  # 指定段のみ強制再実行
    keep_s3: bool = False
    summarize: bool = True  # False=要約段(Bedrock)をスキップし議事録は外部生成に委ねる
    correct: bool = True  # False=LLM 後処理補正(Bedrock)をスキップ（--no-correct, FR-C2-02）


@dataclass(frozen=True)
class PipelineResult:
    status: ResultStatus
    session_id: str
    output_dir: Path
    final_transcript_path: Path | None = None
    minutes_path: Path | None = None
    message: str = ""


class PostMeetingPipeline:
    """パイプライン本体。AWS 依存は注入可能（テストは fake を渡す）。"""

    def __init__(
        self,
        config: PipelineConfig,
        s3io: S3Io | None = None,
        transcribe_client: TranscribeClient | None = None,
        summarizer: Any | None = None,
        credential_checker: Callable[[], None] | None = None,
        corrector: Any | None = None,
    ) -> None:
        self._config = config
        self._s3io = s3io
        self._transcribe = transcribe_client
        self._summarize = summarizer if summarizer is not None else summarize_mod.summarize
        # C2 補正（辞書→LLM）。テストは fake を注入。既定は correction.correct。
        self._correct = corrector if corrector is not None else correction_mod.correct
        # 課金段の前に資格情報の有効性を確認するチェッカ（FR-H2-07）。テストは注入で差し替える。
        self._cred_checker = credential_checker

    # ------------------------------------------------------------------
    def run(self, options: PipelineOptions) -> PipelineResult:
        rec = options.input
        out_dir = self._config.output_dir / rec.session_id
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = _Paths(out_dir)

        forced = self._with_naming_updates(self._forced_stages(options.force, options.target_stage), paths)

        # 後始末は finally で全終了経路（完走・命名待ち停止・例外）に対し1回保証する
        # （ISS-13 / BR-H2-S3-01）。Transcribe 失敗など mid-pipeline 例外でも当該 session の
        # S3 残置（アップロード済み入力 WAV 等）を残さない。delete_prefix は冪等・keep_s3 尊重・
        # フェイルセーフのため、課金前停止や全段再利用で S3 未使用でも安全に通過する。
        # Pass2 はローカル raw を再利用し S3 を参照しないため、停止経路で削除しても再実行に影響しない。
        try:
            # L0: 認証事前チェック（課金段に入る前に1回・全段再利用なら省略, FR-H2-07/BR-H2-AUTH-02）
            if self._aws_will_be_used(rec, paths, forced, summarize=options.summarize):
                self._check_credentials()

            # L3/L4: Transcribe（成果物が無い段のみ起動 → 再課金回避, BR-PIPE-04）
            raw_others, raw_self = self._ensure_transcribed(rec, paths, forced)

            # L5: 統合（merged.json = 実名適用前）
            merged = self._ensure_merged(rec, raw_others, raw_self, paths, forced)

            # L6: 実名割当（マッピング未記入なら停止）。出力は final_transcript.named.json（補正前）。
            named = self._ensure_named(merged, paths, forced)
            if named is None:
                return self._naming_required_result(rec.session_id, paths, vtt=False)

            # L6.5: LLM 後処理補正（C2）。final_transcript.json を補正後で確定（FR-C2-04/05）。
            corrected = self._ensure_corrected(named, options.correct, paths, forced)

            # L7: 議事録生成。--no-summarize 指定時は要約段（Bedrock 課金）をスキップし、
            # final_transcript.json までで停止する。議事録は外部（Claude Code 等）で生成する想定。
            if not options.summarize:
                return self._summarize_skipped_result(rec.session_id, paths)
            self._ensure_summarized(corrected, paths, forced)

            return self._completed_result(rec.session_id, paths, vtt=False)
        finally:
            # L8: 後始末（全経路・keep_s3 で保持）。フェイルセーフなので例外を送出しない。
            self._cleanup(rec, options.keep_s3)

    # ------------------------------------------------------------------
    def run_from_transcript(
        self,
        merged: FinalTranscript,
        *,
        summarize: bool = True,
        correct: bool = True,
        force: bool = False,
        target_stage: Stage | None = None,
    ) -> PipelineResult:
        """統合済みトランスクリプト（VTT 経路など）から命名→議事録のみを駆動する。

        Transcribe/S3 を一切使わない（AWS 課金は C2 補正段と summarize 段の Bedrock。両方外すには
        correct=False かつ summarize=False）。命名ゲートは
        本番経路と共有し、VTT の話者名は merged.model_info["speakerNameHints"] からテンプレ
        初期値に入る（BR-NAME-01）。後始末（S3）も不要なため run() の finally は通さない。
        """
        out_dir = self._config.output_dir / merged.session_id
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = _Paths(out_dir)

        forced = self._with_naming_updates(self._forced_stages(force, target_stage), paths)

        # 統合済み（実名適用前）を永続化し、再実行で再利用できるようにする（FR-16 中間成果物）。
        if not paths.merged.is_file() or bool(forced & {Stage.MERGED}):
            _write_json(paths.merged, merged.to_json())
        else:
            self._verify_same_input(merged, paths)  # 同名の別入力による取り違えを弾く

        # L6: 実名割当（マッピング未記入なら停止）。命名は完全ローカル＝資格情報を要求しない。
        named = self._ensure_named(merged, paths, forced)
        if named is None:
            return self._naming_required_result(merged.session_id, paths, vtt=True)

        # L6.5: LLM 後処理補正（C2）。final_transcript.json を補正後で確定（FR-C2-04）。
        # 補正で LLM を踏む場合は _ensure_corrected 内で資格情報を確認する（FR-C2-08）。
        corrected = self._ensure_corrected(named, correct, paths, forced)

        if not summarize:
            return self._summarize_skipped_result(merged.session_id, paths)

        # L7: 議事録生成。Bedrock を実際に踏むときだけ資格情報を確認する（命名待ち停止時は
        # STS を呼ばない）。minutes 再利用で済む場合も確認は不要（FR-H2-07 と同じ安全側判定）。
        if not (paths.minutes.is_file() and not bool(forced & {Stage.SUMMARIZED})):
            self._check_credentials()
        self._ensure_summarized(corrected, paths, forced)

        return self._completed_result(merged.session_id, paths, vtt=True)

    # ------------------------------------------------------------------
    # 段ごとの load-or-build
    # ------------------------------------------------------------------
    def _ensure_transcribed(
        self, rec: RecordingInput, paths: "_Paths", forced: set[Stage]
    ) -> tuple[RawTranscript, RawTranscript | None]:
        force = Stage.TRANSCRIBED in forced
        raw_others = self._transcribe_role(rec, StreamRole.OTHERS, rec.others_wav_path, paths, force)
        raw_self: RawTranscript | None = None
        if rec.mode == InputMode.PAIRED and rec.self_wav_path is not None:
            raw_self = self._transcribe_role(rec, StreamRole.SELF, rec.self_wav_path, paths, force)
        return raw_others, raw_self

    def _transcribe_role(
        self,
        rec: RecordingInput,
        role: StreamRole,
        wav_path: Path,
        paths: "_Paths",
        force: bool,
    ) -> RawTranscript:
        raw_path = paths.raw(role)
        if raw_path.is_file() and not force:
            logger.info("Transcribe 結果を再利用: role=%s", role.value)
            raw_json = _read_json(raw_path, stage="transcribe")
        else:
            logger.info("Transcribe ジョブを実行: role=%s", role.value)
            raw_json = self._run_transcribe_job(rec, role, wav_path)
            _write_json(raw_path, raw_json)
        return parser.parse(raw_json, role, rec.session_id, rec.language)

    def _run_transcribe_job(self, rec: RecordingInput, role: StreamRole, wav_path: Path) -> dict[str, Any]:
        s3 = self._get_s3()
        s3.verify_preconditions()  # BR-IO-01（暗号化・公開ブロック前提）
        prefix = self._config.s3_prefix
        # single モードのセッションIDは WAV のファイル名（任意文字列）由来。Transcribe の OutputKey は
        # 制限文字集合のため、日本語のファイル名だと **WAV をアップロードした後に** ジョブ開始が
        # ValidationException で落ちる。キーも ASCII へ畳む（mp4→VTT の _safe_id と同方針）。
        safe_id = safe_key_segment(rec.session_id)
        media_key = f"{prefix}{safe_id}/{role.value}.wav"
        output_key = f"{prefix}{safe_id}/transcribe/{role.value}.json"
        media_uri = s3.upload(wav_path, media_key)

        spec = make_job_spec(
            rec.session_id,
            role,
            media_uri,
            rec.language,
            self._config.max_speakers,
            self._config.vocabulary_name,
        )
        self._get_transcribe().run_job(spec, s3.bucket, output_key, self._config.poll_timeout_sec)
        return s3.download_json(output_key)

    def _ensure_merged(
        self,
        rec: RecordingInput,
        raw_others: RawTranscript,
        raw_self: RawTranscript | None,
        paths: "_Paths",
        forced: set[Stage],
    ) -> FinalTranscript:
        force = forced & {Stage.TRANSCRIBED, Stage.MERGED}
        if paths.merged.is_file() and not force:
            logger.info("統合結果(merged)を再利用")
            return FinalTranscript.from_json(_read_json(paths.merged, stage="merge"))
        merged = merger.merge(raw_others, raw_self, rec.common_start_utc, is_partial=rec.is_partial)
        _write_json(paths.merged, merged.to_json())
        return merged

    def _ensure_named(self, merged: FinalTranscript, paths: "_Paths", forced: set[Stage]) -> FinalTranscript | None:
        force = bool(forced & {Stage.TRANSCRIBED, Stage.MERGED, Stage.NAMED})
        if paths.named.is_file() and not force:
            logger.info("実名適用済(named)を再利用")
            return FinalTranscript.from_json(_read_json(paths.named, stage="name"))

        name_map = speaker_label.ensure_naming(merged, paths.naming)
        if name_map is None:
            return None  # テンプレ生成・記入待ちで停止（BR-NAME-01）

        named = speaker_label.apply_mapping(merged, name_map)
        _write_json(paths.named, named.to_json())  # 補正前（原文保持, BR-CORR-04）
        return named

    def _ensure_corrected(
        self,
        named: FinalTranscript,
        correct_enabled: bool,
        paths: "_Paths",
        forced: set[Stage],
    ) -> FinalTranscript:
        """LLM 後処理補正（C2）。final_transcript.json を補正後で確定する（FR-16 連携契約）。

        --no-correct または用語ファイル空/未設定なら無補正で final を確定（後方互換, BR-CORR-03）。
        LLM を実際に踏むときだけ資格情報を確認する（FR-C2-08）。
        """
        force = bool(forced & {Stage.TRANSCRIBED, Stage.MERGED, Stage.NAMED, Stage.CORRECTED})
        if paths.final.is_file() and not force:
            logger.info("補正済(final)を再利用")
            return FinalTranscript.from_json(_read_json(paths.final, stage="correct"))

        if not correct_enabled:
            _write_json(paths.final, named.to_json())  # 無補正で確定
            return named

        terms = correction_mod.load_terms(self._config.correction_terms_path)
        if not terms:
            _write_json(paths.final, named.to_json())  # 用語なし→ no-op
            return named

        self._check_credentials()  # LLM(Bedrock) を踏む前に確認（FR-C2-08）
        outcome = self._correct(named, self._config, terms)
        _write_json(paths.final, outcome.transcript.to_json())
        logger.info(
            "補正完了: status=%s 辞書=%d LLM=%d",
            outcome.status.value,
            outcome.dictionary_hits,
            outcome.llm_changed,
        )
        return outcome.transcript

    def _ensure_summarized(self, named: FinalTranscript, paths: "_Paths", forced: set[Stage]) -> None:
        force = bool(forced & {Stage.TRANSCRIBED, Stage.MERGED, Stage.NAMED, Stage.CORRECTED, Stage.SUMMARIZED})
        if paths.minutes.is_file() and not force:
            logger.info("議事録(minutes)を再利用")
            return
        minutes = self._summarize(named, self._config)
        paths.minutes.write_text(minutes.markdown, encoding="utf-8")
        _write_json(paths.minutes_meta, minutes.to_json())

    def _cleanup(self, rec: RecordingInput, keep_s3: bool) -> None:
        """当該 session の S3 オブジェクトを掃除する（ISS-13 / BR-H2-S3-01）。

        どの終了経路（完走・命名待ち停止・再利用・**例外/失敗**）でも当該 session プレフィックス
        配下を冪等に削除する。前回実行の残置も回収するため、当該実行で S3 未使用でも削除用に
        クライアントを遅延生成する。keep_s3 なら残す。フェイルセーフ: 掃除失敗は警告に
        留め、成果物（final/minutes）を毀損しない。
        """
        if keep_s3:
            return
        # キーは safe_key_segment 済みのIDで組む（_run_transcribe_job と同一。ここがずれると
        # 後始末が別プレフィックスを消しに行き、PII 音声が S3 に残る）。
        prefix = f"{self._config.s3_prefix}{safe_key_segment(rec.session_id)}/"
        try:
            deleted = self._get_s3().delete_prefix(prefix)
            if deleted:
                logger.info("S3 後始末: %d 個のオブジェクトを削除（prefix=%s）", deleted, prefix)
        except Exception as exc:  # noqa: BLE001 フェイルセーフ（BR-H2-S3-01）
            logger.warning("S3 後始末に失敗しました（処理は継続・7日ライフサイクルが保険）: %s", exc)

    def _aws_will_be_used(self, rec: RecordingInput, paths: "_Paths", forced: set[Stage], *, summarize: bool) -> bool:
        """この実行で AWS 課金段（Transcribe/Bedrock）に入る可能性があるか（FR-H2-07）。

        段指定/force があれば AWS を使う可能性あり。そうでなければ「全段再利用」
        （raw 一式と minutes が揃う）でない限り True。事前チェックの過剰実行（STS は無料）は
        許容し、課金前停止の取りこぼしを防ぐ安全側に倒す。
        summarize=False（--no-summarize）の場合は Bedrock 段を踏まないため、AWS が要るのは
        Transcribe 未済（raw 欠落）のときだけ。raw が揃っていれば認証チェックを省く
        （実名適用は完全ローカルのため、議事録外部生成の経路で AWS 資格情報を要求しない）。
        """
        if forced:
            return True
        raws_present = paths.raw(StreamRole.OTHERS).is_file()
        if rec.mode == InputMode.PAIRED and rec.self_wav_path is not None:
            raws_present = raws_present and paths.raw(StreamRole.SELF).is_file()
        if not summarize:
            return not raws_present
        return not (raws_present and paths.minutes.is_file())

    def _check_credentials(self) -> None:
        """資格情報の有効性を確認する（無効なら課金前に actionable 停止, BR-H2-AUTH-02）。"""
        if self._cred_checker is None:
            self._cred_checker = make_credential_checker(self._config.aws_region)
        self._cred_checker()

    # ------------------------------------------------------------------
    # 結果ビルダー（run / run_from_transcript が共有。メッセージ以外は同一構造）
    # ------------------------------------------------------------------
    def _naming_required_result(self, session_id: str, paths: "_Paths", *, vtt: bool) -> PipelineResult:
        """命名ゲート停止（BR-NAME-01）。VTT 経路は話者名初期値の記入を案内する。"""
        hint = (
            "VTT 由来の話者名を初期値に記入済みです。確認・補正して再実行してください。"
            if vtt
            else "実名を記入して再実行してください（Transcribe は再実行されません）。"
        )
        return PipelineResult(
            status=ResultStatus.NAMING_REQUIRED,
            session_id=session_id,
            output_dir=paths.out_dir,
            message=f"話者マッピングファイルを生成しました: {paths.naming}\n{hint}",
        )

    def _summarize_skipped_result(self, session_id: str, paths: "_Paths") -> PipelineResult:
        """--no-summarize で final まで停止（Bedrock 未課金）。議事録は外部生成に委ねる。"""
        return PipelineResult(
            status=ResultStatus.SUMMARIZE_SKIPPED,
            session_id=session_id,
            output_dir=paths.out_dir,
            final_transcript_path=paths.final,
            minutes_path=None,
            message=(
                f"要約段をスキップしました（Bedrock 未課金）。最終トランスクリプト: {paths.final}\n"
                "議事録は外部（Claude Code 等）で生成してください。"
            ),
        )

    def _completed_result(self, session_id: str, paths: "_Paths", *, vtt: bool) -> PipelineResult:
        """全段完走。final_transcript.json と minutes.md を確定して返す。"""
        return PipelineResult(
            status=ResultStatus.COMPLETED,
            session_id=session_id,
            output_dir=paths.out_dir,
            final_transcript_path=paths.final,
            minutes_path=paths.minutes,
            message="パイプライン完了（VTT 経路）。" if vtt else "パイプライン完了。",
        )

    # ------------------------------------------------------------------
    def _forced_stages(self, force: bool, target_stage: Stage | None) -> set[Stage]:
        """再実行対象の段集合を決める（force=全段 / target_stage=その段のみ / 既定=なし）。"""
        if force:
            return set(Stage)
        if target_stage is not None:
            return {target_stage}
        return set()

    def _with_naming_updates(self, forced: set[Stage], paths: "_Paths") -> set[Stage]:
        """speaker_names.json が named より新しければ命名以降を再実行対象に加える。

        命名ゲートの運用は「speaker_names.json を記入・修正して再実行」（BR-NAME-01）。ところが
        段の再利用（Q4=A）は成果物の存在だけを見るため、これが無いと修正が黙って無視され、
        named / final / minutes に古い名前が残ったまま「完了」と報告される（再利用の INFO ログも
        呼び出し側では capture され操作者に見えない）。Stage.NAMED を forced に入れれば、下流
        （corrected / summarized）も既存の force 判定で連鎖して作り直される。
        """
        if _is_newer(paths.naming, paths.named):
            logger.info("話者名の更新を検知したため命名以降を再実行します")
            return forced | {Stage.NAMED}
        return forced

    def _verify_same_input(self, merged: FinalTranscript, paths: "_Paths") -> None:
        """既存の出力ディレクトリが「同じ入力」の成果物かを照合する（別会議の取り違え防止）。

        vtt モードのセッション ID は入力ファイル名（拡張子なし）由来のため、同名の別ファイル
        （Teams 既定の `Recording.vtt`、ライブ字幕由来の `live_captions.vtt` 等）は同じ
        `<output_dir>/<session>/` を共有する。段の再利用はそれを「続きから」と解釈し、前の会議の
        実名・議事録をそのまま成功として返す（内容が静かに取り違えられ、別会議の実名 PII を
        共有し得る）。指紋が違えば続行せず、リネームを促して停止する。
        """
        existing = FinalTranscript.from_json(_read_json(paths.merged, stage="input"))
        if _source_digest(existing) == _source_digest(merged):
            return
        raise PipelineError(
            f"セッション '{merged.session_id}' には別の入力の処理結果が既にあります: {paths.out_dir}\n"
            "セッションIDは入力ファイル名（拡張子なし）由来のため、同名の別ファイルは前の会議の"
            "成果物を再利用してしまいます。入力ファイルを会議ごとに一意な名前へ変更して再実行するか、"
            "既存の出力ディレクトリを退避してください。",
            failed_stage="input",
        )

    def _get_s3(self) -> S3Io:
        if self._s3io is None:
            self._s3io = S3Io(self._config.s3_bucket, self._config.aws_region)
        return self._s3io

    def _get_transcribe(self) -> TranscribeClient:
        if self._transcribe is None:
            self._transcribe = TranscribeClient(self._config.aws_region)
        return self._transcribe


@dataclass(frozen=True)
class _Paths:
    """セッション出力ディレクトリ配下の固定パス群。"""

    out_dir: Path

    def raw(self, role: StreamRole) -> Path:
        return self.out_dir / f"raw_transcribe.{role.value}.json"

    @property
    def merged(self) -> Path:
        return self.out_dir / "final_transcript.merged.json"

    @property
    def named(self) -> Path:
        return self.out_dir / "final_transcript.named.json"  # 命名済・補正前（C2・原文保持）

    @property
    def final(self) -> Path:
        return self.out_dir / "final_transcript.json"  # FR-16 連携契約（補正後）

    @property
    def naming(self) -> Path:
        return self.out_dir / "speaker_names.json"

    @property
    def minutes(self) -> Path:
        return self.out_dir / "minutes.md"

    @property
    def minutes_meta(self) -> Path:
        return self.out_dir / "minutes.meta.json"


def make_credential_checker(region: str) -> Callable[[], None]:
    """STS GetCallerIdentity で資格情報の有効性を確認するチェッカを返す（課金なし, FR-H2-07）。

    SDK 既定のクレデンシャルプロバイダチェーンに委譲し、独自に資格情報を保存しない
    （NFR-SEC-07）。一過性障害は有限回リトライ、恒久障害（期限切れ/権限不足/不明）は
    actionable な PipelineError を送出して課金段の前に停止する（BR-H2-AUTH-02/03）。

    確認そのものは `auth_check.probe_credentials` に委ねる（CLI の `--check-auth` と実装を
    共有し、判定と文言の正本を 1 つに保つため）。ここは「恒久障害なら課金前に止める」という
    段 0 の方針だけを持つ。
    """

    def _check() -> None:
        probe = auth_check.probe_credentials(region)  # boto3 未導入は PipelineError のまま抜ける
        if probe.kind is not None:
            raise PipelineError(auth_policy.remediation_message(probe.kind), failed_stage=auth_check.STAGE)

    return _check


def _is_newer(source: Path, artifact: Path) -> bool:
    """`source` が `artifact` より新しいか（どちらかが欠けていれば False）。"""
    if not source.is_file() or not artifact.is_file():
        return False
    return source.stat().st_mtime > artifact.stat().st_mtime


def _source_digest(transcript: FinalTranscript) -> str:
    """入力の同一性を判定する指紋（セグメントの時刻・話者ラベル・本文のみ・純粋）。

    session_id は含めない（同じ ID に別の入力が来たことを検出するのが目的）。model_info や
    将来のスキーマ追加も含めないため、版差で誤検知しない。ハッシュ化するので比較のために
    本文（PII）を持ち回らない（NFR-SEC-04）。
    """
    canonical = "\n".join(
        f"{seg.start_sec:.3f}\t{seg.end_sec:.3f}\t{seg.speaker}\t{seg.text}" for seg in transcript.segments
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_json(path: Path, *, stage: str) -> dict[str, Any]:
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"中間成果物の読込に失敗しました: {path}", failed_stage=stage) from exc
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
