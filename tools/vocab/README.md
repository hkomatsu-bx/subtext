# tools/vocab — Transcribe カスタム語彙の管理（C1）

固有名詞・人名・専門用語の用語精度を Transcribe Custom Vocabulary で是正する横断ツール。
両ユニット（Unit C ライブ字幕 / Unit B 会議後パイプライン）はここで作る語彙名を各自の設定
で参照するだけで、語彙の管理には関与しない（疎結合・BR-VOCAB-03）。

設計: [docs/design/08-追加機能.md](../../docs/design/08-追加機能.md) の「C1：カスタム語彙」節

## ファイル

- `ja-terms.txt` — 用語ファイル（table 形式・git 管理）。
- `manage_vocabulary.py` — 冪等 upsert（Create/Update）＋ READY 待ちツール。
- `test_manage_vocabulary.py` — 単体テスト（boto3 不要）。

## 用語ファイル書式（`ja-terms.txt`）

TAB 区切り・**4 列ヘッダ必須**（`Phrase` / `SoundsLike` / `IPA` / `DisplayAs`）。

- `Phrase`（必須）: **ASR が聞き取る読み**。ja-JP では言語の文字セット（`ja-jp-character-set`）内のみ。
  英数字は ja-JP 文字セットに**含まれない文字がある**（例: 小文字 `x` は不可）→ 英語・ローマ字の語は
  そのまま入れず**カタカナ読みで書く**（下記「英語・固有名詞」参照）。**空白不可**、数字はスペルアウト。
- `SoundsLike` / `IPA`: **サポート終了のため常に空**（値は無視される）。
- `DisplayAs`（任意）: 出力表記。空なら Phrase をそのまま使う。空白可・数字可。ここは英字・記号可。

例（空欄も TAB を入れる。`Phrase`=読み、`DisplayAs`=出力表記）:

```
Phrase	SoundsLike	IPA	DisplayAs
サブテキスト			Subtext
ビーエックス			BeeX
```

制約: アカウント当たり 100 ファイル / 1 ファイル 50KB / 1 エントリ 256 文字。

### 日本語の用語（ja-JP）

`Phrase`・`DisplayAs` とも日本語可（AWS 公式 charsets で確認）。

- `DisplayAs`: ひらがな・カタカナ・漢字・全角ローマ字大文字を自由に使える。
- `Phrase`: ja-JP 専用文字セット（`ja-jp-character-set`）の範囲内。一般的なひらがな/カタカナ/常用漢字は含まれる。

**英語・固有名詞（重要）**: ja-JP の `Phrase` 列に**英単語をそのまま入れない**。ASR は日本語音声を
カタカナ等で聞き取るため、`Phrase` には**カタカナ読み**を、英語表記は `DisplayAs` に置く。英数字の一部
（例: 小文字 `x`）は ja-JP 文字セット外で、そのまま入れると `CreateVocabulary` が FAILED になる
（実測: `Subtext` の `x` で失敗）。

```
（誤）Subtext			Subtext      ← x が ja-JP 文字セット外 → FAILED
（正）サブテキスト			Subtext      ← Phrase=カタカナ読み / DisplayAs=英語表記
```

例（人名・型番・英語名。空欄も TAB を入れて 4 列を保つ）:

```
小松弘和			小松弘和
イチジー			1G
ビーエックス			BeeX
```

注意点:

1. **`Phrase` に空白不可**（半角・**全角スペース U+3000 も不可**）。日本語は通常スペースなしなので問題になりにくい。`validate_table` が空白混入を検出して弾く。
2. **数字は `Phrase` ではスペルアウト**（数字は `DisplayAs` のみ可）。型番「1G」は `Phrase`=`イチジー`、`DisplayAs`=`1G` のように書く。
3. **稀な漢字・異体字・機種依存文字**は ja-JP 文字セット外の可能性 → その場合 AWS が `CreateVocabulary` を `FAILED` にする（スクリプトが `FailureReason` を提示）。ローカル検証は文字セット照合まではしないため、最終確認は投入時。
4. ファイルは **UTF-8** で保存する。

出典: [Character sets for custom vocabularies - Amazon Transcribe](https://docs.aws.amazon.com/transcribe/latest/dg/charsets.html)

## 実行（投入）

認証は AWS 既定の解決チェーンに委譲する（環境変数 / SSO / プロファイル）。資格情報は書かない。

```bash
uv run --with boto3 python tools/vocab/manage_vocabulary.py \
  --name subtext-ja \
  --bucket <処理バケット名> \
  --file tools/vocab/ja-terms.txt \
  --region ap-northeast-1 --language ja-JP
```

成功で語彙が `READY` になり終了コード 0。`FAILED` / タイムアウトは理由を提示し非ゼロ終了。

投入後、両ユニットで同じ語彙名を設定すると適用される:

- Unit C: `appsettings.json` の `Live:VocabularyName` または環境変数 `SUBTEXT_Live__VocabularyName`。
- Unit B: 環境変数 `VOCABULARY_NAME`（または `.env`）。

語彙の状態確認:

```bash
aws transcribe get-vocabulary --vocabulary-name subtext-ja --region ap-northeast-1
```

## テスト

```bash
uv run --with pytest pytest tools/vocab/
```

## 注意（posture）

- 用語に**人名（PII）を含めることは AWS の非推奨**に該当する。自分専用前提で受容（`production-dev-phase`）。
  社外秘の固有名詞を含める場合は別途 posture 判断を要する。
- 用語ファイルは投入時に S3 へアップロードされる。残置が気になる場合は手動削除する
  （本ツールは cleanup を持たない＝低リスク）。
