# CLAUDE.md

Claude Code がこのリポジトリで作業するときの規則。
**設計・仕様は書かない**（`docs/design/` が一次情報）。ここには環境の前提、コマンド、守るべきルールだけを置く。

Subtext は Web 会議の音声を「会議に参加せず」OS 音声層から取得し、会議後にクラウド処理で議事録を生成する PoC。
疎結合な 3 ユニット（Unit A 録音 / Unit B 会議後パイプライン / Unit C ライブ字幕）と、それらを駆動する会議ハーネス（`tools/meeting`）で構成する。

## 着手前に読む文書

変更対象に応じて、**コードより先に**該当する設計書を読む。

| 変更対象 | 先に読む |
|----------|----------|
| 全体構成・ユニット間連携・入出力レイアウト | [docs/design/02-アーキテクチャ.md](docs/design/02-アーキテクチャ.md) |
| `src/capture`, `src/recorder`（Unit A） | [docs/design/03-unit-a-録音.md](docs/design/03-unit-a-録音.md) |
| `src/postmeeting`（Unit B・mp4→VTT・JSONL→VTT） | [docs/design/04-unit-b-会議後パイプライン.md](docs/design/04-unit-b-会議後パイプライン.md) |
| `src/live`（Unit C） | [docs/design/05-unit-c-ライブ字幕.md](docs/design/05-unit-c-ライブ字幕.md) |
| `tools/meeting`（CLI・TUI・コスト台帳・Slack） | [docs/design/06-会議ハーネス.md](docs/design/06-会議ハーネス.md) |
| `infra`（AWS CDK） | [docs/design/07-インフラ設計.md](docs/design/07-インフラ設計.md) |
| 追加機能（A1 / C1 / C2 / H2 / B1 / B1F） | [docs/design/08-追加機能.md](docs/design/08-追加機能.md) |
| セキュリティ・法務・コストのリスク方針 | [docs/design/09-リスクとセキュリティ方針.md](docs/design/09-リスクとセキュリティ方針.md) |
| `FR-xx` / `BR-xxx-xx` / `NFR-SEC-xx` / `Qn=A` の意味 | [docs/design/付録-トレーサビリティ.md](docs/design/付録-トレーサビリティ.md)（唯一の一次情報） |
| 会議ワークフローの実行境界とデータの行き先 | [.claude/skills/subtext-meeting/SKILL.md](.claude/skills/subtext-meeting/SKILL.md)（ガバナンスの「正」） |
| 言語別の書き方 | `docs/programming_guide (C#).md` / `docs/programming_guide (Python).md` |

索引は [docs/design/README.md](docs/design/README.md)。
運用担当者向けの操作手順は `docs/会議議事録作成マニュアル.html`（単一 HTML）。

## 環境の前提

- **Windows 専用**（WASAPI 依存）。C# は `net10.0-windows`、Python は 3.13。
- Python はルートの uv workspace が `src/postmeeting` と `tools/meeting` を**単一 `.venv` に束ねる**。3.13 の固定は git 追跡下の `.python-version`（ルートと `infra/` の 2 つだけ）が担う。workspace member 側には置かない。
- **コマンドはすべてリポジトリルートで実行する**（workspace 化により `cd` は不要）。
- `infra` は重いため workspace に含めない。デプロイ時のみ `infra/` 配下で個別に `uv sync` する。
- 運用専用 PC には `scripts/publish.ps1` の self-contained な `dist/`（`dist/recorder` / `dist/live`）を配る。配布先に .NET SDK は不要だが、**uv とリポジトリ一式は要る**（TUI は publish 対象外）。手順は README「運用専用機へ配る」。

## コマンド

```bash
# 初回セットアップ（uv sync → .env 準備 → data/ 作成）。PowerShell 7 必須（5.1 不可）
pwsh -NoProfile -File scripts/bootstrap.ps1

# ビルドとテスト
dotnet build Subtext.sln -c Release
dotnet test  Subtext.sln --collect:"XPlat Code Coverage"
uv run pytest src/postmeeting
uv run pytest tools/meeting

# 単一テストクラス / メソッドに絞る
dotnet test tests/Subtext.Recorder.Tests --filter "FullyQualifiedName~SyncMathTests"
dotnet test tests/Subtext.Recorder.Tests --filter "DisplayName~ReturnsOrder"

# 配布ビルド（Unit A/C を self-contained で publish）
pwsh -NoProfile -File scripts/publish.ps1
```

会議ワークフロー（`meeting tui` / `record` / `minutes` / `slack`、`subtext-postmeeting`、`subtext-mp4-to-vtt`、`subtext-live-to-vtt`）の起動は SKILL.md に従う。

## 守るルール

### 秘密と PII

- 認証情報・トークンをソースや `appsettings.json` に直書きしない（BR-SEC-01）。環境変数かルートの `.env` で渡す。
- 音声・実名・発言内容を外部へ送信しない。**ログにも出さない**（実名を扱う箇所は件数だけを出す＝BR-NAME-04）。
- `.env` と `data/` をコミットしない。

### 依存

- **バージョンを完全固定する**（NFR-SEC-05）。NuGet 参照は `Version="x.y.z"` 形式で書き、範囲指定しない。公式レジストリのみ使う。
- Python の依存追加は `uv add`。`pip install` は使わない。

### トレーサビリティ

- コメント中の `FR-xx` / `BR-xxx-xx` / `NFR-SEC-xx` / `Qn=A` は設計書を参照する識別子。**コードを変えたら該当コメントの ID 整合を保つ**。
- `BR-*` はユニットごとに独立採番（同名が別ユニットで別内容）、`Qn=X` はステージごとに独立採番。参照するときはユニット・ステージを併記する。
- 設計・要件が動く変更では、**先に該当する設計書を更新**してからコードに触る。

### コード

- `<Nullable>enable</Nullable>` / `ImplicitUsings` 有効。値オブジェクトは `record` にして不変を保つ。
- コメントは日本語。新規コードは周囲の密度・命名・`FR`/`BR` 参照スタイルに合わせる。
- **ユニット間の疎結合を壊さない**（FR-16）。連携は保存ファイルの受け渡しだけに限り、共有ライブラリ・共有設定を作らない。
- 依頼範囲外のコードを「改善」しない。無関係なデッドコードに気付いたら、削除せず指摘に留める。

### テスト

- テストコードを確認なく削除・コメントアウトしない。
- Unit A の C# テストは WASAPI 実機を使わない。差し替えの継ぎ目は `IAudioCapture` 抽象と `SyncRecorder.RecordAsync` の `startUtcOverride` の 2 か所。

### 実行してよい操作の境界

- **AWS 課金を伴う実行**（Transcribe / Bedrock）は台帳（`cost-ledger.jsonl`）への記録を伴う経路で行う。閾値超過は警告のみで止まらないため、額を確認して報告する。
- **Slack 投稿は外部公開**にあたる。投稿前にプレビューを示し、人間の承認（CLI は `y/N`、TUI はモーダル）を得るまで投稿しない。
- **録音とライブ字幕は Claude Code のセッション内から起動できない**（WASAPI を掴めない）。ユーザーに通常のターミナルで実行してもらう。
- 話者名ゲートは「`spk_0` は誰か」を人間が確定するための安全弁。**埋めずに通過させない**。
