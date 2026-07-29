# Unit A：音声キャプチャ／録音

Unit A は、会議に参加せずに自分と相手の音声を 2 系統で取得し、時間同期を取って WAV に保存する。
言語は C#（net10.0-windows）である。
WASAPI に依存するため Windows 専用になる。

## 責務

Unit A の出力は、会議後パイプライン（Unit B）の主入口になる。
そのため Unit A は「取得」と「正規化・同期・保存」を確実に行い、下流が扱いやすい形の成果物を残すことに専念する。
文字起こしや要約といったクラウド処理は持たない。

## コンポーネント構成

Unit A は 2 つのプロジェクトからなる。

- **capture（Subtext.Capture、クラスライブラリ）**：ハードウェアからの音声取得に専念する。NAudio に依存する。Unit C と共用する。正規化・同期はしない（責務分離、Q5=A）。
- **recorder（Subtext.Recorder、コンソール CLI）**：録音のオーケストレーション。capture を参照する。正規化・同期・保存を担う。

capture の抽象境界は `IAudioCapture` である。
実機実装は `WasapiAudioCapture` で、`DataAvailable` イベントを `Channel` 経由で `IAsyncEnumerable<AudioFrame>` に変換し、各フレームに取得時刻 `captureUtc` を付ける。
テストはこのインターフェースを合成実装に差し替える継ぎ目（seam）として使う。

この `Channel` は容量 1024 フレーム（約 10 秒分）の有界チャネルで、満杯時は最古のフレームを捨てる。
消費側（recorder）が一時的に停滞しても取得側をブロックさせないための設計判断である。
捨てられたフレームは音声の欠落として現れるが、後段の同期が `captureUtc` を見て無音補填するため、時間軸のずれには育たない（BR-SYNC-02）。
つまり Unit A の音声欠落には 2 つの経路がある。
1 つはここで見た取りこぼしで、もう 1 つはループバックが無音時にデータを出さないことである（後者は後述の絶対時刻アンカー同期で扱う）。

recorder の主要な構成要素は次のとおりである。

- **AudioNormalizer**：生フレームを 16kHz / mono / 16bit PCM へ正規化する。順序は「ダウンミックス → リサンプル → 量子化（飽和）」に固定する（BR-FMT-02）。入力は変えず、新しいフレームを返す。対応する入力は IeeeFloat/32bit・PCM/16bit・PCM/32bit で、それ以外（24bit PCM など）とチャンネル数 0 以下は `NotSupportedException` にする。
- **SyncMath**：副作用のない純粋計算。`captureUtc` を唯一の真実として、t0 起点の累積サンプル位置から無音補填量を算出する。同期ロジックの本体がここに集約され、単体テストの主対象になる。
- **SyncRecorder**：SyncMath と WavWriter を使い、2 系統を互いに待たせず絶対位置へ書き込む。片系統が落ちると両系統を停止し、部分保存してから manifest を出力する。
- **WavWriter**：ヘッダを終了時に確定する。異常終了でも途中までの WAV が有効に残る。データ長は 32bit で表すため上限があり、超過を書き込み前に検査して停止する（16kHz / mono / 16bit で約 18.6 時間相当）。`maxDurationMinutes` をこれを超える値に設定しても録音は完走しない。
- **RecordingModels**：連携契約。`SidecarMeta` と `RecordingManifest` は不変（record）で表す。JSON は camelCase・enum 文字列（`self`／`others`、`complete`／`incomplete`）に固定する。
- **DeviceResolver**：self／others の取得デバイスを決定する（FR-06、BR-DEV-01〜03）。名前指定があれば完全一致を要求し、重複する名前があればエラーにする。未指定なら当該方向の既定デバイスを採り、既定が無ければ先頭の候補にフォールバックする。
- **RecorderConfig**：`appsettings.json`・環境変数（接頭辞 `SUBTEXT_`）・コマンドライン引数から設定を束ねる（後勝ち：CLI 引数 > 環境変数 > `appsettings.json`）。

### 設定とコマンドライン引数

設定項目は次の 6 つである。

| キー | CLI 引数 | 既定 | 用途 |
|------|----------|------|------|
| `Recorder:OutputDir` | `--out`／`-o` | `data/recordings` | 出力先ディレクトリ |
| `Recorder:MaxDurationMinutes` | `--minutes`／`-m` | 180 | 自動停止までの分数（BR-STOP-02） |
| `Recorder:StopFile` | `--stop-file` | 未設定 | 停止ファイルのパス。指定時のみ停止経路が有効になる |
| `Recorder:InputDevice` | `--input` | 未設定 | self（マイク）のデバイス名 |
| `Recorder:OutputDevice` | `--output` | 未設定 | others（ループバック元）のデバイス名 |
| `Recorder:AwsRegion` | なし | — | Unit A では使わない（設定の形を Unit C と揃えるためだけに存在する） |

上表の写像を経ずに `--Recorder:キー=値` の形で直接指定することもできる。
`appsettings.json` の `OutputDir` は `data/recordings` だが、設定ファイルを置かずに起動した場合のクラス既定は `recordings` である。

起動時に `MaxDurationMinutes` が 0 以下、または `OutputDir` が空なら例外で停止する（fail-fast、終了コード 2）。
録音を始めてから設定ミスに気づく事態を避けるためである。

## ドメインエンティティ

- **AudioDevice**：取得対象デバイス。ID・名前・方向（`render`／`capture`）・既定フラグを持つ。self はマイク（capture 方向）、others はループバック元（render 方向）。名前は設定によるデバイス指定の突合キーになる。
- **AudioFrame**：キャプチャ音声の最小チャンク。PCM・フォーマット・`captureUtc` を持つ（record として不変）。`captureUtc` は先頭サンプルの捕捉時刻で、同期の唯一の真実になる（BR-SYNC-01）。
- **AudioFormat**（値オブジェクト）：サンプルレート・チャンネル数・ビット深度・サンプル型。正規化後は 16000 / 1 / 16 / PCM に固定する。
- **StreamRole**（enum）：`self`（マイク）と `others`（ループバック）。
- **SidecarMeta**（連携契約）：各 WAV に 1 つ。WAV パス・系統・開始時刻・フォーマット・デバイス名・録音長・無音補填秒を持つ。
- **RecordingManifest**（連携契約）：セッション単位。`sessionId`・作成時刻・`streams`（SidecarMeta ×2）・`status`・`commonStartUtc` を持つ。`streams` は self と others がちょうど 2 件。

## 中核ロジック：絶対時刻アンカー同期

2 系統の同期が Unit A の設計上の山場である。
ループバックは無音時にデータを出さないことがあり、そのままだと相手側 WAV が壁時計より短くなって同期がずれる。

対策として、各フレームの `captureUtc` を同期の唯一の真実に置く（BR-SYNC-01、Q2=A）。
録音開始時刻 t0 を基準に、あるべきサンプル位置を `expectedSamples = round((captureUtc − t0) × 16000)` で求める。
正のギャップ（データが足りない区間）は無音 PCM で補填し、負のギャップ（微小な重なり）は連続書き込みで吸収する。
これにより drift の累積を防ぐ。

無音補填を発火させる閾値 `GAP_THRESHOLD` は定数にする（BR-SYNC-03、マジックナンバーを避ける）。
既定は 20ms 相当の 320 サンプルである。
補填した無音は系統ごとに `silenceFilledSec` に累積し、事後にユーザーが混入や欠落を主観確認できるようにする（BR-SYNC-05、BR-QLT-02）。

先頭フレームだけは扱いが違う。
t0 から最初のフレームまでの先行ギャップは閾値に関わらず全量を補填し、`silenceFilledSec` には数えない（BR-SYNC-05）。
これは録音開始からの開始オフセットであって「欠落の補填」ではないためである。
`captureUtc` が t0 より前だった場合は 0 に丸める。

リサンプルの連続性は系統ごとに持ち回す。
`AudioNormalizer` は状態を持たず、線形補間の端数位置と直前サンプルを呼出側から受け取って更新後の状態を返す。
これにより、フレームを跨ぐ切り捨ての累積と境界の不連続を防ぐ。
状態の保持は `SyncRecorder` の系統ごとの書込器が担う。

混入除去のコード処理は実装しない（BR-QLT-01、スコープ外）。
通知音や他アプリ音の混入は、通知を切る・他アプリを止めるといったユーザー運用で抑える前提を採る。
ただし相手系統が全区間無音だった場合だけは警告を出す（BR-QLT-03）。
ループバック元のデバイスを取り違えていた、あるいは会議音声が別デバイスへ出ていた場合を、議事録の段まで持ち込ませないためである。
判定は正規化後に 0 以外のサンプルを 1 つでも書いたかで行い、無音補填分は信号に数えない。

## 停止とエラー処理

正常停止の経路は 3 つある。

- **手動停止**：Ctrl+C（BR-STOP-01）。
- **自動停止**：経過時間が最大録音時間 `maxDurationMinutes`（既定 180 分）に達したとき（BR-STOP-02、Q1=B）。
- **停止ファイルの検知**：`--stop-file` または環境変数 `SUBTEXT_Recorder__StopFile` でパスを指定する。既定は未設定で、指定時のみ有効になる。

いずれの正常停止でも、残りのバッファを書き切って WAV ヘッダを確定する（BR-STOP-03）。

このうち実運用の正本は停止ファイルである（FR-H2-02）。
Git Bash などシェル経由の SIGINT は即殺（TerminateProcess 相当）になって `finally` の flush が走らず、manifest を生成できない。
会議ハーネスは常に `--stop-file` を渡して起動し、`meeting stop` でそのファイルを作る。
Ctrl+C はコンソールを直接掴んで起動した場合の手段として残す。

停止ファイルの経路には 2 つの補助動作がある。
起動時に前回の残骸（stale なファイル）があれば削除し、起動直後の誤停止を防ぐ。
検知は 250ms 間隔のポーリングで行う。

録音中の致命的異常（デバイス切断・排他競合・書き込み失敗）は fail-fast で停止する（BR-ERR-01、Q7=A）。
片系統が落ちると `linked.Cancel()` で両系統を止め、`finally` で両 WAV を確定（部分保存）してから manifest を出力し、例外を送出する。
異常時は `manifest.status=incomplete` を記録し、下流が識別できるようにする（BR-ERR-03）。
`status` が `incomplete` になるのはキャプチャの異常だけではない。
キャプチャが正常でも WAV の確定に失敗すれば `incomplete` になる。
なお manifest 自体の書き込みが失敗した場合は manifest が残らず、WAV のみが部分保存された状態になる。

エラーメッセージには秘密情報・認証情報・フルパスなどの不要情報を含めない（BR-ERR-04、NFR-SEC-04）。

出力先の上書きも異常として扱う。
セッションディレクトリが既に存在する場合は、録音を始める前に停止する（BR-IO-01）。
`sessionId` は秒精度（`yyyyMMdd-HHmmss`）のため、同一秒での多重起動や同一 ID での再実行が衝突しうる。
既存の録音を黙って壊さないことを優先した。

終了コードの意味は次のとおりである。

- `0`：正常。
- `1`：録音中の致命的異常（部分保存済み）。
- `2`：起動時またはその他の異常（設定不整合・デバイス不在・出力先の衝突を含む）。

起動時には採用したデバイス名と「既定／指定」の別を表示する（BR-DEV-04）。
あわせて、オーケストレータが状態を把握できるよう `sessionId=` と `stopFile=` の機械可読な 1 行も出す。

## テスト容易性の継ぎ目

テストのための継ぎ目が 2 つある。

- `IAudioCapture` 抽象。WASAPI 実機を合成実装に差し替える。
- `SyncRecorder.RecordAsync` の `startUtcOverride`。録音開始時刻 t0 を注入する。

テストは WASAPI 実機を除く純粋ロジックに集中する。
対象は次のとおりである。

- 正規化（`AudioNormalizerTests`）
- 同期計算（`SyncMathTests`）
- デバイス解決（`DeviceResolverTests`）
- モデルの往復変換（`RecordingModelsTests`）
- WAV ヘッダとサイズ上限（`WavWriterTests`）
- 設定の優先順位（`RecorderConfigLoaderTests`）
- 合成ストリームでの録音（`SyncRecorderTests`）
