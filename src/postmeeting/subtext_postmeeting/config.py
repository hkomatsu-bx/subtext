"""パイプライン設定（PipelineConfig・FR-15）。

`.env`（任意）＋環境変数から束縛する。AWS 認証情報は読み込まない — AWS プロファイル/
SSO/環境変数に委譲する（NFR-SEC-07, BR-SEC-01）。必須値の欠落は起動時に fail-fast。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import PipelineError

_STAGE = "config"

# .env の探索を環境変数より優先しない（既存の環境変数を上書きしない）。
_DEFAULTS = {
    "AWS_REGION": "ap-northeast-1",
    "S3_PREFIX": "subtext/jobs/",
    "LANGUAGE": "ja-JP",
    "MAX_SPEAKERS": "5",
    "POLL_TIMEOUT_SEC": "1800",
    "KEEP_S3": "false",
    "OUTPUT_DIR": "out",
    # ③付帯資料。抽出テキストの総文字数上限（要約プロンプトへ入れる資料の合計）。超過分は
    # ファイル単位で末尾から除外する（FR-MAT-04）。
    "MATERIALS_MAX_TOTAL_CHARS": "40000",
    # CORRECTION_TERMS_PATH は CWD 非依存にしたいため _DEFAULTS には置かず、
    # 未設定時はリポジトリ同梱位置を _default_terms_path() で解決する（C2・FR-C2-06）。
}


@dataclass(frozen=True)
class PipelineConfig:
    """全段に注入される不変設定。"""

    aws_region: str
    s3_bucket: str
    s3_prefix: str
    language: str
    max_speakers: int
    poll_timeout_sec: int
    keep_s3: bool
    output_dir: Path
    bedrock_model_id: str  # 空可。summarize 段で必須チェック（SC-P2 先行検証を妨げない）
    vocabulary_name: str  # 空可（C1・FR-C1-02）。未設定なら Transcribe にカスタム語彙を渡さない
    correction_terms_path: Path  # C2・FR-C2-06。不在/空なら後処理補正は no-op
    # 使う AWS プロファイル名。空＝未指定（SDK 既定＝default）。既定値を `default` にしては
    # ならない理由は aws.apply_profile の docstring を参照。
    aws_profile: str = ""
    materials_max_total_chars: int = 40_000  # ③付帯資料。FR-MAT-04

    @staticmethod
    def from_env(env_file: Path | None = None, *, require_s3: bool = True) -> "PipelineConfig":
        """環境変数（必要なら .env を先読み）から設定を構築する。

        s3_bucket は Transcribe/S3 段に必須のため既定で起動時に検証する。VTT 経路は
        Transcribe/S3 を一切踏まないため require_s3=False で検証を外せる（VTT→Claude 生成は
        完全ローカル）。bedrock_model_id は議事録段でのみ必要なため、ここでは空を許容し
        summarize 段で検証する（Q7=A の SC-P2 先行検証では Bedrock 有効化前でも起動できる）。
        """
        values = _load_env(env_file)

        def get(key: str) -> str:
            # 空文字/空白のみの値は「未設定」とみなし既定へ倒す（AWS_REGION="" 等で
            # boto3 生成時に不可解なエラーになるのを防ぐ）。既定を持たないキー（S3_BUCKET・
            # BEDROCK_MODEL_ID 等）は空のまま返し、各段の必須チェックに委ねる。
            value = values.get(key, "")
            if not value.strip() and key in _DEFAULTS:
                return _DEFAULTS[key]
            return value

        s3_bucket = get("S3_BUCKET").strip()
        if require_s3 and not s3_bucket:
            raise PipelineError(
                "S3_BUCKET が設定されていません。.env か環境変数で指定してください。",
                failed_stage=_STAGE,
            )

        return PipelineConfig(
            aws_region=get("AWS_REGION").strip(),
            s3_bucket=s3_bucket,
            s3_prefix=_normalize_prefix(get("S3_PREFIX")),
            language=get("LANGUAGE").strip(),
            max_speakers=_parse_int(get("MAX_SPEAKERS"), "MAX_SPEAKERS"),
            poll_timeout_sec=_parse_int(get("POLL_TIMEOUT_SEC"), "POLL_TIMEOUT_SEC"),
            keep_s3=_parse_bool(get("KEEP_S3")),
            output_dir=Path(get("OUTPUT_DIR").strip() or "out"),
            bedrock_model_id=get("BEDROCK_MODEL_ID").strip(),
            vocabulary_name=get("VOCABULARY_NAME").strip(),
            correction_terms_path=(
                Path(terms_env) if (terms_env := get("CORRECTION_TERMS_PATH").strip()) else _default_terms_path()
            ),
            # 環境変数 → .env の順（_load_env の規則）。未設定は空のまま（既定へ倒さない）。
            aws_profile=get("AWS_PROFILE").strip(),
            materials_max_total_chars=_parse_int(
                get("MATERIALS_MAX_TOTAL_CHARS"), "MATERIALS_MAX_TOTAL_CHARS"
            ),
        )

    def require_bedrock_model(self) -> str:
        """議事録段でのみ呼ぶ。モデル ID 未設定なら fail-fast（FR-15）。"""
        if not self.bedrock_model_id:
            raise PipelineError(
                "BEDROCK_MODEL_ID が設定されていません。議事録生成には必須です。",
                failed_stage="summarize",
            )
        return self.bedrock_model_id


def _default_terms_path() -> Path:
    """C2 補正用語ファイルの既定パスを CWD 非依存で解決する（FR-C2-06）。

    解決順:
      1. リポジトリ同梱（`<Subtext.sln のある階層>/tools/correction/terms.json`）。
         親ディレクトリ数を直書きせず目印ファイルを上位探索する（ディレクトリ再編に強い）。
      2. 従来のパッケージ位置基準（後方互換のフォールバック）。

    解決に失敗すると**補正が黙って no-op になる**（ログは INFO 1行で、ハーネスは子の stdout を
    capture するため操作者には見えない）。Unit B は uv workspace 経由でのみ起動するため、
    `Subtext.sln` は常に上位に存在する。
    """
    here = Path(__file__).resolve()
    for base in here.parents:
        if (base / "Subtext.sln").is_file():
            return base / "tools" / "correction" / "terms.json"
    return here.parents[3] / "tools" / "correction" / "terms.json"


def _load_env(env_file: Path | None) -> dict[str, str]:
    """環境変数を基本とし、.env があれば未設定キーのみ補完する（環境変数を上書きしない）。"""
    merged: dict[str, str] = dict(os.environ)
    path = env_file if env_file is not None else Path(".env")
    if path.is_file():
        for key, value in _parse_env_file(path).items():
            merged.setdefault(key, value)
    return merged


def _parse_env_file(path: Path) -> dict[str, str]:
    """最小限の .env パーサ（KEY=VALUE、# コメント、空行を許容）。"""
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _normalize_prefix(prefix: str) -> str:
    cleaned = prefix.strip().strip("/")
    return f"{cleaned}/" if cleaned else ""


def _parse_int(value: str, key: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise PipelineError(f"{key} は整数で指定してください: '{value}'", failed_stage=_STAGE) from exc
    if parsed <= 0:
        raise PipelineError(f"{key} は正の整数で指定してください: {parsed}", failed_stage=_STAGE)
    return parsed


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}
