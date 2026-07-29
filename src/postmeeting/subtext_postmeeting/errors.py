"""パイプラインのエラー型（BR-ERR・Q9=A fail-fast）。

例外は握りつぶさず、どの段で失敗したかの文脈を付けて送出する（BR-ERR-01/03）。
メッセージには音声内容・実名(PII)・AWS 認証情報を含めない（BR-ERR-04, NFR-SEC-04）。
"""

from __future__ import annotations


class PipelineError(Exception):
    """会議後パイプラインの致命的エラー。

    failed_stage に失敗段を保持し、CLI は非ゼロ終了で段を明示する（BR-ERR-03）。
    呼び出し側は機微情報をメッセージに載せないこと（BR-ERR-04）。
    """

    def __init__(self, message: str, *, failed_stage: str) -> None:
        super().__init__(message)
        self.failed_stage = failed_stage

    def __str__(self) -> str:
        return f"[{self.failed_stage}] {super().__str__()}"
