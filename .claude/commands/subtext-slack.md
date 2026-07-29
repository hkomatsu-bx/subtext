---
description: 生成済み議事録(data/out/<session>/minutes.md)を Slack へ投稿する。要約を親メッセージ、全文をスレッド返信。投稿先は実行時指定
argument-hint: "[session] [#channel]"
model: opus
allowed-tools: Read, Glob, Bash(ls:*), mcp__plugin_slack_slack__slack_search_channels, mcp__plugin_slack_slack__slack_send_message
---

`subtext-meeting` スキルのステップ4（Slack 投稿）。

これは **Claude が実行する**段。ただし外部公開のため、**宛先チャンネルと親メッセージ案を提示し、
ユーザーの承認を得てから**投稿する。

手順:
1. セッションを解決し `data/out/<session>/minutes.md` を読む。
   - `$ARGUMENTS` にセッションがなければ `data/out/` の最新を提案しユーザー確認。
   - `minutes.md` が無ければ「先に `/subtext-minutes` を完了してください」と案内して止まる。
2. 投稿先チャンネルを確定する（**実行時に毎回指定**）:
   - `$ARGUMENTS` に `#channel` 指定があればそれ。無ければユーザーに尋ねる。
   - `slack_search_channels` で実在を確認し、チャンネル ID を得る。
3. **親メッセージ（要約）**を組み立てる。minutes.md から以下を簡潔に抽出:
   - 会議名 / 日時 / 出席者
   - 決定事項
   - ToDo（担当・期限が分かれば併記）
4. 投稿前に「投稿先チャンネル」と「親メッセージ案」を提示し、**承認を求める**。
5. 承認後:
   - `slack_send_message` で親メッセージ（要約）を投稿し、`thread_ts` を取得。
   - 同じスレッドに `minutes.md` 全文を返信する。Slack の文字数上限を超える場合は
     見出し境界で分割し、複数返信に分ける。
6. 投稿後、メッセージの permalink（取得できれば）と投稿件数を報告する。

注意: 音声・PII を含む内容を外部へ出す操作。チャンネルの取り違えがないか必ず確認すること。
