---
description: Unit B 議事録パイプライン（Transcribe→Bedrock または Claude 生成）の実行コマンドを確定・検証して提示する（実行はユーザー）。Claude 生成なら `--no-summarize`。話者名ゲートも案内
argument-hint: "[session 既定:recordings最新] [--stage summarized | --no-summarize]"
model: opus
allowed-tools: Bash(ls:*), Bash(test:*), Read, Glob, Grep
---

`subtext-meeting` スキルのステップ3（議事録生成）。

**Claude はパイプラインを実行しない。** Transcribe バッチ + Bedrock の AWS 課金が発生するため、
叩くのはユーザー本人。Claude はコマンド確定・ドライラン検証・話者名記入の支援を行う。

**推奨提示（runner）**: （リポジトリルートで）`uv run meeting minutes [<session>] [--claude]`。
見積（今回 Transcribe 概算 / 今月累計 / 上限）を表示 → パイプライン自動実行 →
`tools/meeting/cost-ledger.jsonl` へ概算を追記 → 単発/月次が閾値超過なら警告（停止はしない）。
Claude 経路（`--no-summarize`）は `--claude`。以下の `subtext-postmeeting` 直叩きは runner を
使わない場合の代替。Slack 投稿も runner に含む（`uv run meeting slack` / `meeting process` 末尾）。
どの経路でも投稿前の人間承認は必須で、Claude に文面を作らせたい場合は `/subtext-slack` を使う。

議事録の生成バックエンドは2通り（どちらも Transcribe は必要）:
- **(既定) Bedrock 生成**: パイプラインが `minutes.md` まで自動生成。トランスクリプトは自社 AWS 内に留まる。
- **Claude 生成（`--no-summarize`）**: パイプラインは `final_transcript.json` で停止し Bedrock を呼ばない。
  Claude（このセッション）が `final_transcript.json` を読んで `minutes.md` を生成する。
  **注意（データ送信先）**: この経路は実名入りトランスクリプト（PII）が Anthropic 側へ送信される。
  Bedrock 経路（自社 AWS の日本リージョン内＝`ap-northeast-1`/`ap-northeast-3` に留まる）との違いを
  理解した上で選ぶこと。どちらを使うかユーザーに確認する。

手順:
1. セッションを解決する。`$ARGUMENTS` にセッション名があればそれ、無ければ `data/recordings/` の
   最新ディレクトリを提案しユーザー確認。`--stage` 等の追加フラグがあれば引き継ぐ。
2. ドライラン検証（読み取りのみ。**.env の値は出力しない** — BR-SEC-01）:
   - `data/recordings/<session>/manifest.json` が存在するか。
   - **リポジトリルートの** `.env` に `S3_BUCKET` と `BEDROCK_MODEL_ID` が**キーとして**設定済みか
     （`grep -E '^(S3_BUCKET|BEDROCK_MODEL_ID)=' .env` の有無のみ確認、値は伏せる）。
3. 次のコマンドを提示する（**ユーザーが実行**。AWS 課金）。**リポジトリルートで実行**（workspace 化により
   cd 不要）。`<session>` は解決済みセッション名:

   ```bash
   # bash/pwsh 共通（既定: Bedrock 生成）
   uv run subtext-postmeeting --mode paired --session data/recordings/<session>
   ```
   Claude 生成を選ぶ場合は末尾に `--no-summarize` を付ける（Bedrock を呼ばない）:
   `uv run subtext-postmeeting --mode paired --session data/recordings/<session> --no-summarize`
4. **話者名ゲート（BR-NAME-01）**: 初回実行は `data/out/<session>/speaker_names.json` を生成して
   `NAMING_REQUIRED` で停止する。求められたら:
   - `data/out/<session>/final_transcript.merged.json` を読み、話者ラベルごとの
     実名候補を提案する（この場合 Claude が本文を読む＝実名 PII が Anthropic へ送信される。社外に出したくなければ手動記入）。
   - ユーザーが `speaker_names.json` を記入後、**同じコマンドを再実行**。
5. 完了処理（バックエンド別）:
   - **Bedrock 経路**: 再実行で `data/out/<session>/minutes.md` が確定する。議事録のみ作り直すなら
     `--stage summarized`（Transcribe を再課金しない）。
   - **Claude 経路（`--no-summarize`）**: 再実行は `final_transcript.json` を生成し `SUMMARIZE_SKIPPED`
     で停止する（`minutes.md` は未生成）。Claude が `data/out/<session>/final_transcript.json`
     を読み、議事録 Markdown を生成して `data/out/<session>/minutes.md` に**書き出す**。
     見出しは Bedrock 版と揃える: `## 決定事項` / `## ToDo`（`- [ ] 内容（担当 / 期限）`形式）/ `## 論点・議論サマリ`。
     トランスクリプトに無い情報を創作しない。partial 録音由来なら冒頭に注記を付す（BR-SUM-05 相当）。
6. 完了後 `data/out/<session>/minutes.md` を確認し、`/subtext-slack <session>` で投稿へ進めることを伝える。
