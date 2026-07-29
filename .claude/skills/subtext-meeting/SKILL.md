---
name: subtext-meeting
description: Subtext の会議ワークフロー統括。録音(Unit A)→議事録(Unit B)→Slack投稿、および任意のライブ字幕(Unit C)を、Claude Code から段階ゲート付きで進める。「会議を録音」「議事録をSlackに」「ライブ字幕」等の依頼で使う。
---

# Subtext 会議ワークフロー

Web 会議の音声を OS 音声層から取得し、会議後に議事録を生成して Slack へ投稿する一連の流れを、
Claude Code から段階ゲート付きで駆動する統括スキル。各段は専用コマンドに対応する。

```
/subtext-record  → (録音) → data/recordings/<session>/manifest.json
/subtext-stop    → 録音停止シグナル
/subtext-minutes → (Transcribe→Bedrock) → data/out/<session>/minutes.md
/subtext-slack   → 要約を投稿 + 全文をスレッド返信
/subtext-live    → (任意) 会議中のライブ字幕
```

## ターミナル起点ハーネス（`meeting`, 推奨）

録音→議事録の駆動は**ターミナル起点のオーケストレータ** `meeting`（uv workspace の member
`tools/meeting`, CLI 名 `meeting`）に寄せる。Claude は判断段（話者名提案・議事録生成・Slack 文面）を
担い、状態検知・課金実行・台帳記録は runner が担う。

```bash
# リポジトリルートで実行（別ターミナル推奨。`cd` は不要）
uv run meeting status [session]   # 今どの段か（ファイル存在から検知。手報告を不要にする中核）
uv run meeting record [分]        # 録音開始（必要時のみ自動ビルド→exe 起動）
uv run meeting stop               # 停止ファイル作成（Claude 実行可）
uv run meeting minutes [session] [--claude]  # 見積→自動実行→台帳追記→閾値警告
uv run meeting process [session] [--claude]  # 録音済→議事録→(任意)Slack を対話1コマンドで駆動
uv run meeting slack [session] --channel <id>  # 生成済み議事録を Slack 投稿（要約=親/全文=スレッド）
uv run meeting cost [--month YYYY-MM]        # コスト台帳の月次サマリ
uv run meeting live                          # ライブ字幕（確定字幕を JSONL 永続化。B1F）
uv run meeting tui                           # 同じ操作を画面で（ライブ字幕の開始を除く）
                                             #   取込(i)は VTT/mp4 の両方、話者名の記入もモーダルで完結
```

Unit B の起動は `uv run subtext-postmeeting` の一本（dist 経路は無い）。`scripts/publish.ps1` が
生成するのは `dist/recorder` / `dist/live` の 2 つ。配布先にも uv とリポジトリ一式が要る
（`docs/design/06-会議ハーネス.md`）。

**ガバナンス（厳守）**:
- **課金（Transcribe/Bedrock）・PII 送信（Claude 経路）は自動実行可**。ただし `minutes` は
  実行のたびに `tools/meeting/cost-ledger.jsonl`（追記のみ）へ概算を記録し、単発 / 月次累計が
  閾値（既定 単発 $5 / 月次 $50, `meeting.toml`）を超えると**警告**する（停止はしない）。
  Claude 経路は `piiSent:true` を必ず残す。金額は概算で、正は AWS Cost Explorer。
- **Slack 投稿は runner に含む**（`meeting slack` / `process` 末尾）。ただし**外部公開のため
  投稿前にプレビューと y/N 承認を必須**とする（人間承認を残す）。Bot Token は環境変数→ルート
  `.env`（`SLACK_BOT_TOKEN`）の順で解決し、値はログに出さない（BR-SEC-01）。Claude/Anthropic を
  経由せず PII は自社内・Slack のみ。従来の `/subtext-slack`（Claude 文面生成経由）も選択肢として残す。
- 録音・ライブ字幕の実機 WASAPI 制約は runner でも回避できない。`meeting record` も
  **通常のターミナルで**実行する（Claude セッション内 `!` 実行は起動初期で必ず失敗する）。

以降の各ステップは runner（`meeting ...`）を第一提示とし、従来の exe / `subtext-postmeeting`
直叩きは「runner を使わない場合の代替」として残す。

## 最重要: 実行境界（厳守）

Claude が**直接実行してはならない**コマンドがある。実機 WASAPI を掴む録音・ライブ字幕、
および AWS 課金が発生する処理（Transcribe / Bedrock）は、**ユーザー本人が叩く**。
Claude の役割は「コマンドを確定して提示し、パス・env・入力の整合（ドライラン）を検証する」ところまで。

| 段 | Claude 実行 | 理由 |
|----|:---:|------|
| 録音 (Unit A) | ❌ 提示のみ | 実機マイク/ループバック取得（WASAPI） |
| ライブ字幕 (Unit C) | ❌ 提示のみ | 実機取得 + Transcribe Streaming 課金 |
| 議事録 (Unit B) | ❌ 提示のみ | Transcribe バッチ + Bedrock 課金 |
| 停止シグナル | ✅ 可 | 停止ファイルを置くだけ（無害） |
| 整合チェック | ✅ 可 | ファイル/設定の読み取り検証のみ |
| Slack 投稿 | ✅ 可（投稿前に確認） | 外部公開のため宛先と内容を必ず確認 |

各コマンドの実行モデルは frontmatter の `model:` で指定済み（オーバーライドはそのターンのみ有効）:
定型段（record / stop / live）は **sonnet**、判断・外部公開を伴う段（minutes / slack）は **opus**。
`effort` は未指定でセッション値を継承する。このスキル自体はモデルを固定しない。

提示コマンドはユーザーがコピペで叩けるコードブロックで出す。**bash と PowerShell(pwsh) を併記する**
（ユーザー環境は Windows + bash/pwsh 併用のため）。両シェルで同一のコマンドは「bash/pwsh 共通」と注記して1つで足りる。
シェルで差が出るのは主に環境変数指定（bash `VAR=val cmd` / pwsh `$env:VAR='val'; cmd`）・行継続（`\` / `` ` ``）・
exe 直叩きのパス表記。セッション内で叩いてもらう場合はプロンプトに `! <command>` を入力する手があると案内する。

## 標準フロー

### ステップ1: 録音（→ /subtext-record）
- 作業ディレクトリはリポジトリのクローン先ルート（`Subtext.sln` のある階層）。絶対パスは使わない。
- 停止ファイルは `data/recordings/.stop` を既定とする。
- 最大録音分・停止ファイルは**コマンド引数**で渡す（env も併用可・CLI 引数が優先。未指定なら appsettings の 180）。
- **推奨（runner）**: 別ターミナルで（リポジトリルートで）`uv run meeting record <MIN>`。
  exe は publish 出力 `dist/recorder` を優先し、無ければ `src/recorder/bin` を自動探索、それも無ければ
  自動で `dotnet build` する。停止ファイルは `data/recordings/.stop`。
- 代替（runner を使わない場合）。self-contained exe を事前に publish する:

  ```bash
  # bash — 事前に一度: pwsh -NoProfile -File scripts/publish.ps1
  dist/recorder/Subtext.Recorder.exe \
    --minutes <MIN> --stop-file data/recordings/.stop --out data/recordings
  ```
  ```powershell
  # pwsh — 事前に一度: pwsh -NoProfile -File scripts/publish.ps1
  .\dist\recorder\Subtext.Recorder.exe `
    --minutes <MIN> --stop-file data/recordings/.stop --out data/recordings
  ```
  引数: `--minutes`/`-m`, `--stop-file`, `--out`/`-o`, `--input`/`--output`（`--Recorder:Key=値` 形式も可）。
  代替（bash/pwsh 共通・要 .NET SDK）: `dotnet run --project src/recorder -c Release -- --minutes <MIN> --stop-file data/recordings/.stop --out data/recordings`（毎回ビルド）。
- ドライラン検証: `src/recorder/Subtext.Recorder.csproj` と 実行可能な exe（`dist/recorder` か `src/recorder/bin` 配下）の存在、`data/recordings/` 書込可、`.stop` の stale 有無。
- 出力: `data/recordings/<yyyyMMdd-HHmmss>/{self.wav, others.wav, *.meta.json, manifest.json}`。
  `manifest.json` が Unit B の唯一の入口（FR-16）。

### ステップ2: 停止（→ /subtext-stop）
- `data/recordings/.stop` を作成する（Claude 実行可）。録音プロセスがこれを検知して安全停止し manifest を確定。
- runner: `uv run meeting stop`（同じく停止ファイルを置くだけ）。

### ステップ3: 議事録生成（→ /subtext-minutes）
- **入力は 4 経路**ある。録音（`--mode paired`）が主経路で、ほかに会議サービスの WebVTT 直接
  （`--mode vtt --vtt <file>`、FR-17）、録画 mp4（`subtext-mp4-to-vtt` で VTT 化してから合流、FR-18）、
  ライブ字幕 JSONL（`subtext-live-to-vtt` で VTT 化してから合流、FR-B1F-05）がある。
  VTT 経路は Transcribe と S3 を使わず、`--no-summarize` なら AWS 課金ゼロ。
  TUI では「取込」ボタン（`i`）で VTT と mp4 の両方を同じように扱える（`.mp4` は自動で mp4→VTT 段を
  通してから VTT 経路へ合流。尺は ffprobe で測って見積を出す）。以下は録音経路の手順を書く。
- セッションを解決（未指定なら `data/recordings/` の最新ディレクトリを提案、ユーザー確認）。
- **生成バックエンドは2通り**（どちらも Transcribe は必要。どちらを使うかユーザーに確認）:
  - **(既定) Bedrock 生成**: パイプラインが `minutes.md` まで自動生成。トランスクリプトは自社 AWS 内に留まる。
  - **Claude 生成（`--no-summarize`）**: `final_transcript.json` で停止し Bedrock を呼ばない。Claude が
    それを読んで `minutes.md` を生成。**実名入り PII が Anthropic 側へ送信される**点が Bedrock 経路
    （自社 AWS の日本リージョン内＝`ap-northeast-1`/`ap-northeast-3` に留まる）との決定的な違い。
    ガバナンス判断の上で選ぶ。
- **推奨（runner）**: （リポジトリルートで）`uv run meeting minutes [<session>] [--claude]`。
  見積（今回 Transcribe 概算 / 今月累計 / 上限）を表示 → パイプラインを自動実行 →
  `tools/meeting/cost-ledger.jsonl` へ概算を追記 → 閾値超過なら警告。`--claude` で Claude 生成経路（`--no-summarize`）。
  runner は出力先 `data/out` を `OUTPUT_DIR` として自動注入する。
  NAMING_REQUIRED で停止したら案内に従い話者名を記入して再実行（`meeting process` と `meeting tui` は
  記入と再実行をその場で行うため、この往復が不要）。
- 代替（runner を使わない場合。**ユーザーが実行**。AWS 課金。Claude 生成時は末尾に `--no-summarize`）。
  **リポジトリルートで実行**（workspace 化により cd 不要）:

  ```bash
  # bash/pwsh 共通
  uv run subtext-postmeeting --mode paired --session data/recordings/<session>
  ```
- ドライラン検証: `data/recordings/<session>/manifest.json` 存在、**ルートの** `.env` に
  `S3_BUCKET`（必須）/ `BEDROCK_MODEL_ID`（Bedrock 経路のみ必須）設定済み、`OUTPUT_DIR`（既定 `data/out`）。
  **.env の値はログに出さない**（BR-SEC-01）。
- **話者名ゲート（BR-NAME-01）**: 初回実行は統合後 `data/out/<session>/speaker_names.json` を生成して
  `NAMING_REQUIRED` で停止する。Claude は `final_transcript.merged.json` を読み、各話者ラベルへの
  実名候補を提案できる（この場合 Claude が本文を読む＝実名 PII が Anthropic へ送信される。社外に出したくなければ手動記入）。ユーザーが `speaker_names.json` を
  記入後、同じコマンドを再実行。
  `meeting process`（対話入力）と `meeting tui`（専用モーダル）は、この記入と再実行をその場で行う
  ため往復が不要。空欄のラベルは元の `spk_n` 表記のまま残り、既記入と `self`（既定「自分」）は
  温存される。無確認の自動記入はしない（FR-H2-03）。
- 完了処理（バックエンド別）:
  - **Bedrock 経路**: 再実行で `data/out/<session>/minutes.md` 確定。議事録のみ作り直すなら `--stage summarized`。
  - **Claude 経路**: 再実行は `final_transcript.json` を生成し `SUMMARIZE_SKIPPED` で停止。Claude が
    それを読み議事録 Markdown を生成し `data/out/<session>/minutes.md` へ書き出す。見出しは Bedrock 版と統一
    （`## 決定事項` / `## ToDo` / `## 論点・議論サマリ`）。トランスクリプトに無い情報は創作しない。

### ステップ4: Slack 投稿（→ `meeting slack` または /subtext-slack）
- **推奨（runner・ローカル完結）**: `uv run meeting slack [session] --channel <id>`。
  親=構造化サマリ（日時タイトル＋`■ 決定事項`番号付き＋`■ 主なToDo`＋脚注）、全文=スレッド分割返信。
  投稿前にプレビュー→**y/N 承認**。`process` の最後でも投稿を提案する。Bot Token は `.env` の
  `SLACK_BOT_TOKEN`、既定チャンネルは `meeting.toml` の `[slack] default_channel`。GFM→Slack mrkdwn
  変換込み。**Claude/Anthropic を経由しない**（PII は自社内・Slack のみ・課金なし）。
- 代替（`/subtext-slack`・Claude 文面生成経由。Slack MCP が必要）:
  - `data/out/<session>/minutes.md` を読む。投稿先チャンネルは**実行時に毎回指定**（引数 or プロンプト）。
  - スタイル: **要約を親メッセージ**（会議名・日時・出席者・決定事項・ToDo を簡潔に）、
    **全文 minutes.md をスレッド返信**。長文は分割。
  - 投稿前にチャンネルと親メッセージ案をユーザーに提示し、承認を得てから `slack_send_message` を実行。
  - 投稿後、メッセージ permalink を報告する。

## 任意: ライブ字幕（→ /subtext-live）
- 会議中のリアルタイム確認用。既定では保存ファイルを介さないが、B1F の JSONL sink を有効にすると
  確定字幕が永続化され、**議事録生成の入口にもなる**（Unit B とは依然ファイル受け渡しのみ）。
- **推奨（runner）**（**ユーザーが実行**。実機取得 + Transcribe Streaming 課金。Ctrl+C で停止）:

  ```bash
  uv run meeting live                              # bash/pwsh 共通
  ```
  専用セッションを採番し、`SUBTEXT_Live__CaptionSinkPath` と `SUBTEXT_Live__StopFilePath` を注入して
  `data/out/<session>/live_captions.jsonl` に確定字幕を追記する。終了時に議事録化の手順を表示する。
  停止ファイルは `data/out/<session>/.stop`（録音用の `data/recordings/.stop` とは別。`meeting stop` では止まらない）。
- 代替（runner を使わない場合。JSONL は残らず、議事録化には繋がらない）:

  ```bash
  dotnet run --project src/live -c Release          # bash/pwsh 共通
  ```
- 会議後に議事録へ繋ぐ（JSONL → VTT → 既存の命名ゲート）:

  ```bash
  uv run subtext-live-to-vtt data/out/<session>/live_captions.jsonl --out data/out/<session>/<session>.vtt
  uv run subtext-postmeeting --mode vtt --vtt data/out/<session>/<session>.vtt
  ```
  cue の並びは `captureUtc` 基準（A1 の扱いは `docs/design/08-追加機能.md`）。
  **`--out` でセッション毎に一意なファイル名を与えること**。`vtt` モードのセッションIDは入力ファイル名の
  stem なので、既定の `live_captions.vtt` をそのまま使うと会議ごとに同じ出力ディレクトリを共有してしまう
  （Unit B は指紋照合で別入力を弾いて停止するため、2 回目以降はリネームを求められる）。
  話者名は Unit C の表示ラベル（「自分」「相手」）が候補として入るので、命名ゲートで必ず実名へ直す。
- 再接続が不安定なら原因を stderr に出せると案内（bash: `SUBTEXT_DIAG_RECONNECT=1 dotnet run ...` /
  pwsh: `$env:SUBTEXT_DIAG_RECONNECT=1; dotnet run ...`）。

## 参照
- ユニットの責務・契約と設計は `docs/design/`（索引は `docs/design/README.md`。ユニット間の連携契約は
  `docs/design/02-アーキテクチャ.md`、`FR`/`BR`/`NFR`/`Q` の定義は `docs/design/付録-トレーサビリティ.md`）。
  規約とコマンドは `CLAUDE.md`。運用担当者向けの手順書は
  `docs/会議議事録作成マニュアル.html`（単一 HTML。Markdown 版は廃止）。
- 検証補助ツール: `tools/verify/analyze_recording.py`, `tools/verify/validate_minutes.py`。
- カスタム語彙（任意・C1）: `tools/vocab/manage_vocabulary.py`、env `VOCABULARY_NAME` / `SUBTEXT_Live__VocabularyName`。
