---
description: Unit A 録音を開始する起動コマンドを確定・ドライラン検証して提示する（実行はユーザー）
argument-hint: "[最大録音分 既定180]"
model: sonnet
allowed-tools: Bash(ls:*), Bash(test:*), Read, Glob
---

`subtext-meeting` スキルのステップ1（録音開始）。

**Claude は録音を実行しない。** 実機 WASAPI を掴むため、叩くのはユーザー本人。
Claude はコマンドを確定して提示し、ドライラン（パス・env・引数の整合）だけ行う。

**推奨提示（runner）**: 別ターミナルで（リポジトリルートで）`uv run meeting record <最大録音分>`。
必要時のみ自動ビルドして exe を起動する（停止ファイルは `data/recordings/.stop`）。
exe は publish 出力 `dist/recorder` を優先し、無ければ `src/recorder/bin` 配下を自動探索する
（TargetFramework は直書きしない）。
Claude セッション内 `!` 実行は起動初期で必ず失敗するため、必ず通常のターミナルで叩いてもらう。
以下の exe 直叩きは runner を使わない場合の代替として提示する。

手順:
1. 最大録音分 = `$ARGUMENTS`（未指定なら 180）に確定する。
2. ドライラン検証（読み取りのみ）:
   - `src/recorder/Subtext.Recorder.csproj` が存在するか。
   - 実行可能な exe（`dist/recorder/Subtext.Recorder.exe` または `src/recorder/bin` 配下のビルド出力）が
     存在するか。無ければ先に `dotnet build src/recorder -c Release` か `pwsh -NoProfile -File scripts/publish.ps1` が必要な旨を伝える。
   - `data/recordings/` が存在するか（なければ初回作成される旨を伝える）。
   - `data/recordings/.stop` が残っていないか（stale なら録音側起動時に削除されるが警告する）。
3. 次のコマンドを**コピペで叩ける形**で提示する（プロジェクトルートで実行・**別ターミナル推奨**）。
   最大録音分・停止ファイルは**コマンド引数**で渡す（env も併用可・CLI 引数が優先）:

   ```bash
   # bash — 事前に publish（初回/コード変更後のみ）: pwsh -NoProfile -File scripts/publish.ps1
   # 録音開始（self-contained exe 直叩き。引数: --minutes / -m, --stop-file, --out / -o）
   dist/recorder/Subtext.Recorder.exe \
     --minutes <確定値> \
     --stop-file data/recordings/.stop \
     --out data/recordings
   ```
   ```powershell
   # pwsh — 事前に publish（初回/コード変更後のみ）: pwsh -NoProfile -File scripts/publish.ps1
   # 録音開始
   .\dist\recorder\Subtext.Recorder.exe `
     --minutes <確定値> `
     --stop-file data/recordings/.stop `
     --out data/recordings
   ```

   - 引数の対応: `--minutes`/`-m`=最大録音分, `--stop-file`=停止ファイル, `--out`/`-o`=出力ルート,
     `--input`/`--output`=デバイス名。`--Recorder:MaxDurationMinutes=<値>` の完全キー形式も可。
   - 従来どおり環境変数（`SUBTEXT_Recorder__MaxDurationMinutes` 等）でも指定でき、両指定時は CLI 引数が勝つ。
   - exe を使わず `dotnet run --project src/recorder -c Release -- --minutes <確定値> --stop-file data/recordings/.stop --out data/recordings`
     としてもよい（`--` 以降が録音プログラムへの引数。毎回ビルドが走る代替。要 .NET SDK）。
4. 停止は Ctrl+C / `/subtext-stop`（停止ファイル作成）/ 上限到達のいずれか、出力は
   `data/recordings/<yyyyMMdd-HHmmss>/manifest.json` であることを伝える。

セッション内で叩く場合はプロンプトに `! <上記コマンド>` を入れる手があると案内してよい。
