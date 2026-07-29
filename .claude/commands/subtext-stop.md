---
description: 録音停止シグナル（data/recordings/.stop）を作成して進行中の録音を安全停止させる
model: sonnet
allowed-tools: Bash(ls:*), Bash(touch:*), Bash(test:*), Write
---

`subtext-meeting` スキルのステップ2（録音停止）。

停止ファイルを置くだけなので **Claude が実行してよい**（録音プロセスがこれを検知して
WAV をフラッシュし manifest を確定する）。

runner を使う場合は（リポジトリルートで）`uv run meeting stop` でも同じ停止ファイルを作れる。

手順:
1. `data/recordings/` 配下に進行中の録音があるか軽く確認する（最新セッションディレクトリ）。
2. `data/recordings/.stop` を作成する:

   ```bash
   # bash
   touch data/recordings/.stop
   ```
   ```powershell
   # pwsh
   New-Item -ItemType File -Force data/recordings/.stop | Out-Null
   ```
3. 録音側コンソールに `Saved to data/recordings/<session> (status=complete)` が出れば停止完了。
   その後 `/subtext-minutes` で議事録生成に進めることを伝える。
4. 停止ファイルは録音側が次回起動時に自動削除する。残留が気になる場合は手動削除も可。
