# tools/verify — 受け入れ検証の解析補助（ISS-04/05）

実 AWS 受け入れ検証（SC-P1/P3/P4・ISS-05）の出力を**客観的に解析**するための補助スクリプト群。
製品コードではなく検証ヘルパー（`src/` の各ユニットの出力を読むだけ）。**標準ライブラリのみ・依存ゼロ**。
`SC-Pn`／`ISS-nn` の定義は [docs/design/付録-トレーサビリティ.md](../../docs/design/付録-トレーサビリティ.md) 第 6 節。

| スクリプト | 対象 | 入力 | 役割 |
|---|---|---|---|
| `analyze_recording.py` | SC-P1 | `data/recordings/<session>` or `manifest.json` | status/2系統/16k-mono-16bit/長さ整合/無音補填率を機械判定 |
| `validate_minutes.py` | SC-P3 | `minutes.md` or `data/out/<session>` | 必須3セクション/非空/ToDo形式/截断/部分録音注記 |
| `analyze_live_latency.py` | SC-P4/ISS-05 | ライブ出力キャプチャ `.txt` | 系統別遅延分布/partial・final比/フリーズ候補/サマリ突合 |

## 実行

リポジトリルートから実行する（uv workspace で単一 `.venv` に統合済み＝cd 不要）。

```bash
uv run python tools/verify/analyze_recording.py data/recordings/<session>
uv run python tools/verify/validate_minutes.py data/out/<session>
uv run python tools/verify/analyze_live_latency.py live_capture.txt
```

> `python` を直接呼ばないこと。Windows では Microsoft Store のスタブが `python` を占有していることがあり、
> **何も実行せずに終了する**（エラーに見えないため気づきにくい）。必ず `uv run python` を使う。

## 設計上の約束（NFR-SEC-04）

- 音声・字幕・議事録の**本文を一切出力しない**（数値・件数・有無のみ）。Claude へはこれらの出力のみ渡す。
- 終了コード: `0`=機械判定 PASS（要 ▲ 人手確認）, `1`=NG/不合格, `2`=引数エラー。

## 受け入れ検証の手順

ユーザーが実マイク／実 AWS で実行し、出力（PII を含まない解析レポート）を Claude が解析して go/no-go を判定する分担。

前提を 3 つ守る。

- **ビルド済 `.exe` を直起動する**（`dotnet run` は Ctrl+C がアプリに届かず、manifest/WAV ヘッダの確定と
  遅延サマリの出力が走らない。ISS-14）。
- **`--verbose` は使わない**（一時トークンがログに出る。ISS-12。デバッグが要るときは秘匿環境のみ）。
- 資格情報は SDK 既定のプロバイダチェーンに委ねる（`awscrt` 導入済のため SSO/login プロファイルを直接解決できる。
  ISS-11）。解決できないときのみ `aws configure export-credentials` をフォールバックに使う。

### 1. SC-P1 録音／同期（Unit A）

コスト最小化のため、ここで録ったセッションを SC-P3 にも使う（Transcribe バッチは 1 回で済む）。

```powershell
dotnet build Subtext.sln -c Release
$env:SUBTEXT_Recorder__maxDurationMinutes = '2'
.\src\recorder\bin\Release\net10.0-windows\Subtext.Recorder.exe
Remove-Item Env:\SUBTEXT_Recorder__maxDurationMinutes   # 後続セッションに漏らさない
uv run python tools/verify/analyze_recording.py data/recordings/<session>
```

`▲` の人手確認（会議に参加せず取得できたか・混入が許容範囲か）は所見を添える。

### 2. SC-P3 構造化議事録（Unit B）

上の録音を paired で流す（初回は話者名ゲートで停止 → 記入 → 再実行）。

```bash
uv run subtext-postmeeting --mode paired --session data/recordings/<session>
#   1回目: data/out/<session>/speaker_names.json を生成して NAMING_REQUIRED 停止
#   実名（非機密なら役割名でも可）を記入して再実行
uv run subtext-postmeeting --mode paired --session data/recordings/<session>
uv run python tools/verify/validate_minutes.py data/out/<session>
```

議事録だけ作り直すなら `--stage summarized`（Transcribe は再課金されない）。

### 3. SC-P4 ＋ ISS-05 ライブ字幕の遅延／挙動（Unit C）

**実会議ではなく非機密のテスト発話で行う**（字幕本文をファイルに残すため。NFR-SEC-04）。

キャプチャの取得だけはシェル依存で、**PowerShell 7（pwsh）を前提**にする。
Windows PowerShell 5.1 のリダイレクトと `Tee-Object` は UTF-16 で書き出し、解析スクリプト（UTF-8 固定読み）が読めない。

```powershell
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
.\src\live\bin\Release\net10.0-windows\Subtext.Live.exe 2>&1 | Tee-Object -FilePath live_capture.txt
#   30秒〜1分ほど発話したら Ctrl+C → 末尾に「遅延サマリ: ...」が出れば正常終了
uv run python tools/verify/analyze_live_latency.py live_capture.txt
Remove-Item live_capture.txt   # テスト発話でも本文は残さない
```

5.1 しか無い場合は cmd 経由でバイト列をそのまま落とす。

```powershell
cmd /c ".\src\live\bin\Release\net10.0-windows\Subtext.Live.exe > live_capture.txt 2>&1"
```

### 4. 後始末（PII 削除）

```powershell
Remove-Item -Recurse -Force data/recordings/<session>, data/out/<session>, live_capture.txt -ErrorAction SilentlyContinue
aws s3 ls s3://<BucketName>/ --recursive   # 残置ゼロを確認（ISS-13 は H2 で恒久対策済）
```

### 5. Claude へ渡すもの（PII を出さない）

| 検証 | 渡すもの | 渡さないもの |
|---|---|---|
| SC-P1 | `analyze_recording.py` の出力＋▲所見 | WAV 本体・会話内容 |
| SC-P3 | `validate_minutes.py` の出力＋可読性所見 | minutes.md 本文 |
| SC-P4/ISS-05 | `analyze_live_latency.py` の出力＋体感所見 | 字幕本文・キャプチャファイル |

`<session>` は実際のセッション名（例 `20260624-103000`）へ置換して実行する。
PowerShell では `<` `>` をそのまま渡すとパースエラーになる。
