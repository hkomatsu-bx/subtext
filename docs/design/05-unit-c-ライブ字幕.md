# Unit C：ライブ字幕

Unit C は、会議中にリアルタイムで字幕を出す。
言語は C#（net10.0-windows）で、Amazon Transcribe Streaming を使う。
会議後パイプライン（Unit B）とは独立して動く。
既定では保存ファイルを介さないが、B1F を有効化した場合のみ確定字幕を JSONL に永続化する。

## 責務

Unit C の目的は、会議中の即時確認である。
自分と相手の 2 系統に分けて字幕を出し、遅延と暫定結果（partial）の安定性を観測できるようにする。
相手の中の個人区別は行わない。
これは会議後パイプライン（Unit B）の話者分離が担う責務であり、Unit C では系統ラベル（「自分」「相手」）までに留める（BR-STREAM-03、Q4=A）。

この責務分担は当初の設計判断に基づく。
Transcribe のリアルタイム話者分離が日本語の個人識別に対応していないため、ライブでは系統粒度に留め、個人区別は会議後のバッチに寄せた。

## コンポーネント構成

Unit C は capture（Subtext.Capture）を Unit A と共有し、その上に live プロジェクト（Subtext.Live）を持つ。
主要な構成要素は次のとおりである。

- **LivePcmConverter**：AudioFrame を Transcribe Streaming が受け取る PCM 形式へ変換する純粋処理。Unit A のコードには手を入れない。
- **ILiveTranscribeClient / TranscribeStreamingClient**：Transcribe Streaming への接続。話者分離は使わない。言語とサンプルレートは設定値で、既定は `ja-JP` と 16000 である。
- **CaptionRenderer**：字幕をコンソールに出す。遅延を算出して併記する。
- **LiveSttApp**：2 系統を並行駆動するオーケストレータ。
- **ReconnectPolicy**：再接続の判断（例外分類・バックオフ・予算超過）を担う純粋関数群（A1）。
- **ICaptionSink / JsonlCaptionSink**：確定字幕の永続化シンク（B1F）。未注入なら永続化しない。
- **LiveSttConfig**：Unit A の `RecorderConfig` とは独立した設定（FR-15、Q5=A）。

### 設定

`appsettings.json`（必須。欠けると起動しない）と環境変数（接頭辞 `SUBTEXT_`）から束ねる。

| キー | 環境変数 | 既定 | 用途 |
|------|----------|------|------|
| `Live:AwsRegion` | `SUBTEXT_Live__AwsRegion` | なし（必須） | Transcribe Streaming のリージョン |
| `Live:Language` | `SUBTEXT_Live__Language` | なし（必須。`appsettings.json` は `ja-JP`） | 認識言語 |
| `Live:SampleRate` | `SUBTEXT_Live__SampleRate` | 16000 | 送信サンプルレート |
| `Live:Sources` | `SUBTEXT_Live__Sources` | `self,others` | 対象系統（片方だけの指定も可、BR-CAP-02） |
| `Live:VocabularyName` | `SUBTEXT_Live__VocabularyName` | 未設定 | カスタム語彙名（C1、FR-C1-01）。未設定なら語彙なし |
| `Live:CaptionSinkPath` | `SUBTEXT_Live__CaptionSinkPath` | 未設定 | 確定字幕 JSONL の出力先（B1F、FR-B1F-01） |
| `Live:StopFilePath` | `SUBTEXT_Live__StopFilePath` | 未設定 | 停止ファイルのパス（B1F、FR-B1F-03） |
| `Live:Reconnect:*` | `SUBTEXT_Live__Reconnect__*` | 初回 1000ms・上限 30000ms・予算 5 分・keep-alive 5000ms | 再接続の設定（A1、NFR-A1-01） |

`AwsRegion` と `Language` が未設定なら起動時に例外で止まる（fail-fast）。
秘密は設定に置かない（BR-SEC-01、NFR-SEC-07）。

## ドメインエンティティ

- **StreamSource**（enum）：`Self`（自分／マイク）と `Others`（相手／ループバック）。
- **LiveCaption**（値）：1 字幕イベント。話者・テキスト・partial フラグ・オフセット・`captureUtc` を持つ（遅延はフィールドに持たず、表示層が受信時刻と `captureUtc` から算出する）。
- **LatencySample**（値）：遅延観測の 1 標本。系統・オフセット・遅延を持つ。final のうち `captureUtc` を算出できたものだけを標本にする。

## 中核ロジック

capture の共有ライブラリで self / others の AudioFrame を取得し、`captureUtc` を遅延算出の基準にする（BR-CAP-03）。

Transcribe が申告する結果の開始オフセットは**送信した音声の累積時間**であり、壁時計ではない。
両者は一致しない。
keep-alive（FR-A1-05）は capture が途絶している間 5 秒あたり 20ms しか送らないため、
ループバックが静かな間（相手が黙っている通常状態）は音声時間が壁時計の 0.4% しか進まない。
「セッション先頭のアンカー ＋ オフセット」で復元すると、無音 1 分で約 60 秒、10 分で約 10 分ずれる。
マイク側（self）はフレームが連続するためほとんどずれず、系統間のインターリーブが壊れる
（`subtext-live-to-vtt` は `captureUtc` 昇順で並べるため、相手の cue が数分早い位置へ回り込む）。

そこで送信済み音声の「音声時間 ↔ 壁時計」の対応を持つ（`AudioTimeline`）。
基準点は**最後に送ったチャンク**（実フレームは捕捉時刻、keep-alive の無音は注入時刻）で、書き込みごとに更新する。
無音区間で開いた差はその都度解消されるため、誤差は「連続して音声が流れている区間の中」に収まり、履歴を持たないのでメモリは定数である。
遅延は実測ではなく、この復元値に基づく推定である。

系統ごとに独立した Transcribe Streaming 接続を張り、2 系統を並行して送信する（BR-STREAM-01、Q1=A）。
互いに待たせない。
サンプルレート（既定 16000）・PCM（16bit LE mono）で送る（BR-STREAM-02）。
無音区間の分割は Transcribe 側に委ね、VAD は持たない（BR-STREAM-04、NFR-06）。

送信側は capture の列挙子と AWS SDK の publisher を `Channel` で分離する。
容量は 100 チャンク（約 2 秒）の有界チャネルで、満杯時は取得側を待たせる（背圧）。
Unit A の取得側チャネルが最古を捨てるのに対し、ここは捨てずに待たせる。
字幕の取りこぼしは体感品質に直結するうえ、2 秒の遅延はライブ字幕として許容できるという判断である。

この分離には構造上の理由もある。
SDK の publisher は列挙子を複数回進めようとする実装があり、capture の `IAsyncEnumerable` を直接渡すと例外になる。
チャネルを挟むことで、列挙子に触れるのは 1 つのタスクだけになる。

字幕はコンソールに最小表示する（BR-RENDER-01、FR-13）。
GUI は持たない。
永続ファイルは既定では持たず、B1F の JSONL sink を有効化した場合のみ確定字幕を書き出す（[08-追加機能.md](08-追加機能.md) を参照）。
partial と final はいずれも新規行で出力し、同一行の上書きはしない（BR-RENDER-02、Q2=B）。
partial には暫定印を付す。
各行に系統ラベルとオフセットを併記する（BR-RENDER-03）。

再接続が起きた系統では、境界を示す行を 1 行出す。
再接続後のオフセットは新しいセッションの相対時刻として 0 起点に戻るため、この行がないと読み手が時刻を読み違える。

各字幕の遅延を `受信・確定時刻 − captureUtc` で算出して表示する（BR-LAT-01、Q3=A）。
final の字幕を観測対象として記録し、セッション終了時に件数・平均・最大の最小サマリを表示してよい（BR-LAT-02／03）。

## 終了コードと診断

終了コードの意味は次のとおりである。

- `0`：正常（Ctrl+C による停止を含む）。
- `1`：実行中の致命的異常。
- `2`：起動時または設定の異常。

再接続が不安定なときの診断用に、環境変数で出力を増やせる。
`SUBTEXT_DIAG_RECONNECT=1` で例外の分類と原因の連鎖を stderr に出し、`SUBTEXT_DIAG_STACK=1` で完全なスタックトレースを出す。
既定ではいずれも出さない（パスや内部構造の漏洩を避ける）。

## 再接続耐性（A1 による上書き）

当初の Unit C は fail-fast の一括停止を採り、片系統が異常終了したら両系統を止めていた（BR-ERR-03）。
これは PoC が遅延と partial 安定性の観測を目的としたためで、自動再接続を実装しなかった（BR-ERR-02、Q6=A）。

自分専用の実用段階で、この方針を A1（再接続耐性）が部分的に上書きした。
一過性の障害は系統ごとに自動再接続し、恒久障害でも当該系統のみ止めて生存系統を継続する。
詳細は [08-追加機能.md](08-追加機能.md) を参照する。

## 用語精度（C1 による拡張）

固有名詞の誤認識を減らすため、Transcribe のカスタム語彙を使える（C1）。
`Live:VocabularyName` に AWS 上で作成済みの語彙名を指定したときだけ、接続要求に語彙名を付ける（FR-C1-01）。
未設定なら語彙なしの従来動作に戻る（BR-VOCAB-02）。
語彙の作成・管理は Unit C の責務ではなく、専用ツールが担う（BR-VOCAB-03）。
日本語では効果がカタカナ固有名詞に限られる。
詳細と限界は [08-追加機能.md](08-追加機能.md) の C1 節を参照する。

## 永続化基盤（B1F による拡張）

Unit C は既定では字幕を永続化しないが、ライブ議事録に向けた基盤スライス B1F が確定字幕の JSONL 永続化を追加する。
B1F は実装済みで、設定 `Live:CaptionSinkPath`（env `SUBTEXT_Live__CaptionSinkPath`）を指定したときのみ確定字幕を JSONL へ追記し、`Live:StopFilePath`（env `SUBTEXT_Live__StopFilePath`）で停止ファイル停止を有効化する。

JSONL の 1 行は `subtext-live-to-vtt` との連携契約である（BR-B1F-IO-01）。
camelCase で `offsetMs`・`source`（`self`／`others`）・`speaker`（`自分`／`相手`）・`text`・`captureUtc`（算出できなければ null）を持つ。
渡すのは final のみで、partial は書かない。
要件と設計は [08-追加機能.md](08-追加機能.md) にまとめる。
