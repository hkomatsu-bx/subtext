---
description: Unit C ライブ字幕（Transcribe Streaming）の起動コマンドを確定・検証して提示する（実行はユーザー）。会議中のリアルタイム確認用
model: sonnet
allowed-tools: Bash(ls:*), Bash(test:*), Read, Glob
---

`subtext-meeting` スキルの任意段（ライブ字幕）。

**Claude は実行しない。** 実機取得 + Transcribe Streaming の課金が発生するため、叩くのはユーザー本人。
主目的は会議中のリアルタイム確認だが、B1F の JSONL sink を有効にすると確定字幕が永続化され、
会議後の議事録生成の入口にもなる。

手順:
1. ドライラン検証（読み取りのみ）: `src/live/Subtext.Live.csproj` の存在を確認。
2. 次のコマンドを提示する（リポジトリルートで実行。Ctrl+C で停止）:

   ```bash
   uv run meeting live                               # bash/pwsh 共通（推奨）
   ```
   ハーネスが専用セッションを採番し、`data/out/<session>/live_captions.jsonl` へ確定字幕を追記する。
   停止ファイルは `data/out/<session>/.stop`（録音用の `data/recordings/.stop` とは別物）。
   JSONL を残さずただ字幕を見るだけなら代替として `dotnet run --project src/live -c Release`。
3. オプション案内（環境変数の指定形式はシェルで異なる）:
   - 再接続が不安定なときの原因診断:
     - bash: `SUBTEXT_DIAG_RECONNECT=1 dotnet run --project src/live -c Release`
     - pwsh: `$env:SUBTEXT_DIAG_RECONNECT=1; dotnet run --project src/live -c Release`
   - カスタム語彙を使う場合（AWS で作成済みのもの）:
     - bash: `SUBTEXT_Live__VocabularyName=<語彙名> dotnet run --project src/live -c Release`
     - pwsh: `$env:SUBTEXT_Live__VocabularyName='<語彙名>'; dotnet run --project src/live -c Release`
4. 会議後の議事録の作り方を伝える。録音していれば主経路（`/subtext-record` → `/subtext-minutes` →
   `/subtext-slack`）を勧める。録音がなくライブ字幕の JSONL だけがある場合は、これを VTT へ変換して
   既存の命名ゲートへ合流させる（`--no-summarize` なら AWS 課金ゼロ）:

   ```bash
   uv run subtext-live-to-vtt data/out/<session>/live_captions.jsonl
   uv run subtext-postmeeting --mode vtt --vtt data/out/<session>/live_captions.vtt
   ```
   字幕確定までの遅延を負うため、精度は録音経路に劣る点も添える。
