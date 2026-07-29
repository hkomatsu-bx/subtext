"""Subtext Unit B — 会議後パイプライン（Python）。

Unit A の出力（WAV + manifest/meta, FR-16）を入力に、Amazon Transcribe バッチ
（self/others 別ジョブ・others のみ話者分離, Q1=A）→ 絶対時刻統合（Q2=A）→
Amazon Bedrock 議事録生成（FR-10/11）を行うローカル CLI パイプライン。

連携契約は WAV と final_transcript.json のみ。Unit A の内部実装には依存しない（BR-IN-05）。
"""

__version__ = "0.1.0"
