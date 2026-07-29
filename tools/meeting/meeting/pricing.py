"""コスト概算ロジック（単価表 + 各段の見積）。

ここでの金額はあくまで「概算」であり、正確な請求額は AWS Cost Explorer を正とする
（`meeting.toml` のヘッダ参照）。pricing は副作用を持たない純粋ロジックに保ち、
toml の読込は config.py に委ねる（`Pricing.from_config` は parse 済み dict を受ける）。

見積の対象:
  - Transcribe: paired モードは self/others の2系統とも課金されるため合算する。
  - Bedrock  : トランスクリプト文字数→入力トークン概算、要約出力は固定トークン概算。
               既定経路（Bedrock 生成）は **要約段の前に C2 用語補正段も Bedrock を踏む**ため、
               補正段（stage="correct"）も独立した見積対象にする（数えないと1回の実行で発生する
               Bedrock 支出の約半分が見積・台帳から漏れる）。
  - Claude   : `--no-summarize` 経路。実名 PII を Anthropic 側へ送信する点が Bedrock と異なる。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

# 概算金額の表示・台帳記録に使う丸め桁（USD）。
_USD_ROUND_DIGITS = 4
# 1分あたりの秒数（Transcribe 課金単位の換算）。
_SECONDS_PER_MINUTE = 60.0
# トークン単価の分母（USD / 1K tokens）。
_TOKENS_PER_PRICE_UNIT = 1000.0


@dataclass(frozen=True)
class Estimate:
    """1段ぶんの概算結果。`usd` は丸め済み、`detail` は人間向けの内訳説明。"""

    stage: str  # "transcribe" / "correct"（C2 用語補正・Bedrock） / "bedrock" / "claude"
    usd: float
    detail: str
    pii_sent: bool = False


@dataclass(frozen=True)
class Pricing:
    """単価表（USD）。改定は `meeting.toml` の1箇所のみ、暗記しない。"""

    transcribe_usd_per_minute: float
    bedrock_input_usd_per_1k: float
    bedrock_output_usd_per_1k: float
    claude_input_usd_per_1k: float
    claude_output_usd_per_1k: float
    chars_per_token: float
    summary_output_tokens: int
    correction_output_tokens: int

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any]) -> "Pricing":
        """parse 済みの meeting.toml 相当の dict から単価表を束縛する。

        欠損キーや不正な型は KeyError / TypeError として伝播させる（握りつぶさない）。
        """
        pricing = cfg["pricing"]
        estimation = cfg["estimation"]
        transcribe = pricing["transcribe"]
        bedrock = pricing["bedrock"]
        claude = pricing["claude"]

        chars_per_token = float(estimation["chars_per_token"])
        if chars_per_token <= 0:
            raise ValueError("estimation.chars_per_token は正の数である必要があります")

        return cls(
            transcribe_usd_per_minute=float(transcribe["usd_per_minute"]),
            bedrock_input_usd_per_1k=float(bedrock["input_usd_per_1k"]),
            bedrock_output_usd_per_1k=float(bedrock["output_usd_per_1k"]),
            claude_input_usd_per_1k=float(claude["input_usd_per_1k"]),
            claude_output_usd_per_1k=float(claude["output_usd_per_1k"]),
            chars_per_token=chars_per_token,
            summary_output_tokens=int(estimation["summary_output_tokens"]),
            correction_output_tokens=int(estimation["correction_output_tokens"]),
        )

    def estimate_transcribe(self, self_sec: float, others_sec: float) -> Estimate:
        """Transcribe バッチの概算。paired は self/others の2系統とも課金される。"""
        if self_sec < 0 or others_sec < 0:
            raise ValueError("録音秒数は非負である必要があります")
        total_minutes = (self_sec + others_sec) / _SECONDS_PER_MINUTE
        usd = _round_usd(total_minutes * self.transcribe_usd_per_minute)
        detail = (
            f"Transcribe: (self {self_sec:.1f}s + others {others_sec:.1f}s)"
            f" = {total_minutes:.2f}分 × ${self.transcribe_usd_per_minute}/分"
        )
        return Estimate(stage="transcribe", usd=usd, detail=detail)

    def estimate_bedrock(self, transcript_chars: int) -> Estimate:
        """Bedrock 要約の概算。トランスクリプト文字数を入力トークンに換算。"""
        usd, detail = self._estimate_llm_call(
            transcript_chars,
            self.bedrock_input_usd_per_1k,
            self.bedrock_output_usd_per_1k,
            label="Bedrock要約",
            output_tokens=float(self.summary_output_tokens),
        )
        return Estimate(stage="bedrock", usd=usd, detail=detail, pii_sent=False)

    def estimate_correct(self, transcript_chars: int) -> Estimate:
        """C2 用語補正段（Bedrock）の概算。要約段とは別の Bedrock 呼び出し。

        補正はトランスクリプト全文をプロンプトに載せ、「訂正した行だけ」を差分 JSON で受け取る
        （correction.py）。したがって入力は要約段と同規模・出力は小さい。既定経路（Bedrock 生成）は
        要約段の前に必ずこの段を踏むため、ここを数えないと実支出の約半分が見積・台帳から漏れる。
        """
        usd, detail = self._estimate_llm_call(
            transcript_chars,
            self.bedrock_input_usd_per_1k,
            self.bedrock_output_usd_per_1k,
            label="Bedrock補正",
            output_tokens=float(self.correction_output_tokens),
        )
        return Estimate(stage="correct", usd=usd, detail=detail, pii_sent=False)

    def estimate_claude(self, transcript_chars: int) -> Estimate:
        """Claude 経路（--no-summarize）の概算。実名 PII を外部送信するため pii_sent=True。"""
        usd, detail = self._estimate_llm_call(
            transcript_chars,
            self.claude_input_usd_per_1k,
            self.claude_output_usd_per_1k,
            label="Claude",
            output_tokens=float(self.summary_output_tokens),
        )
        return Estimate(stage="claude", usd=usd, detail=detail, pii_sent=True)

    def _estimate_llm_call(
        self,
        transcript_chars: int,
        input_usd_per_1k: float,
        output_usd_per_1k: float,
        label: str,
        output_tokens: float,
    ) -> tuple[float, str]:
        """LLM 1 回呼び出し（補正/要約・Bedrock/Claude 共通）の入出力トークン概算 → USD と内訳。"""
        if transcript_chars < 0:
            raise ValueError("文字数は非負である必要があります")
        input_tokens = transcript_chars / self.chars_per_token
        input_usd = input_tokens / _TOKENS_PER_PRICE_UNIT * input_usd_per_1k
        output_usd = output_tokens / _TOKENS_PER_PRICE_UNIT * output_usd_per_1k
        usd = _round_usd(input_usd + output_usd)
        detail = (
            f"{label}: 入力 {transcript_chars}字 ≒ {input_tokens:.0f}tok × ${input_usd_per_1k}/1k"
            f" + 出力 {output_tokens:.0f}tok × ${output_usd_per_1k}/1k"
        )
        return usd, detail


def _round_usd(value: float) -> float:
    """USD 概算の丸め（表示・台帳の一貫性のため1箇所に集約）。"""
    return round(value, _USD_ROUND_DIGITS)
