---
type: reference
title: "C# プログラミングガイド"
description: "C# のコーディング規約（Effective C# / More Effective C# / C# 公式コーディング規約 準拠）"
---

# C# プログラミングガイド

C# のコーディング規約。本プロジェクトで C# コードを書く際は本ガイドに従う。

## 出典と優先順位

複数の規約が衝突する場合、上にあるものを優先する。

1. **参考書『Effective C#: 50 Specific Ways to Improve Your C#』第3版**（Bill Wagner 著, Addison-Wesley, 2017, C# 6.0 対応）。本ガイドの項目番号・原題はこれに準拠し、根拠としては公開された項目タイトルと要点のみを引用する（本文の逐語転載はしない）。本書を `EC` と略す
2. **参考書『More Effective C#: 50 Specific Ways to Improve Your C#』第2版**（Bill Wagner 著, Addison-Wesley, 2017）。同じく項目番号・原題に準拠する。本書を `MEC` と略す
3. **C# 公式ドキュメント（Microsoft Learn）**（<https://learn.microsoft.com/dotnet/csharp/>）— 言語仕様・公式の規範。とりわけ「C# coding conventions」（<https://learn.microsoft.com/dotnet/csharp/fundamentals/coding-style/coding-conventions>）、「識別子の命名規則と規約」、および「.NET Framework Design Guidelines」（<https://learn.microsoft.com/dotnet/standard/design-guidelines/>）。本書群を `MS` と略す
4. プロジェクト共通規約（`.claude/rules/` の common・csharp）およびユーザーグローバル規約

> **例外（プロジェクト固有の決定を優先）**: この順位は設計判断・イディオムに適用する。ただし **バージョン選定・テストフレームワーク・依存ライブラリ・ツールチェーンなどプロジェクト固有の決定は、4 のプロジェクト規約が 1〜3（書籍・公式規範）に優先する**。例: 本プロジェクトのテストフレームワークは xUnit（`.claude/rules/csharp/testing.md`）であり、書籍が別のフレームワークを例示していてもプロジェクトの決定に従う。

### 補助リファレンス（規範ではなく参考）

設計の裏取りや実装の参考に用いる。規約の根拠としては 1〜4 を優先する。

- **.NET アナライザ（Roslyn Analyzers）と `.editorconfig`**（<https://learn.microsoft.com/dotnet/fundamentals/code-analysis/overview>）：静的解析・機械的強制レイヤー。「何を警告し、何を自動修正するか」の根拠（MEC Item 50）
- **Framework Design Guidelines**（Cwalina & Abrams 著）：公開 API 設計の定番リファレンス。命名・型設計・例外の裏取りに用いる（§7 を補強）
- **BenchmarkDotNet**（<https://benchmarkdotnet.org/>）：マイクロベンチマークの事実上の標準。性能最適化の計測根拠（§12）
- **Refactoring カタログ（Martin Fowler）**（<https://refactoring.com/catalog/>）：リファクタリング手法の定番カタログ（§12 の根拠）

> 設計判断（型設計・リソース管理・ジェネリクス・非同期・例外）は必ず Effective C#／More Effective C# の該当項目を根拠にする。言語仕様・命名は MS 公式に従い、機械的強制は Roslyn アナライザ（`.editorconfig`）で行う。補助リファレンスは規範ではなく参考にとどめる。

---

## 規約の読み方

各項目は「規約（命令形）＋ 根拠（`EC Item N` は Effective C# 第3版、`MEC Item N` は More Effective C# 第2版の項目番号）」で記す。
`EC` は Effective C#、`MEC` は More Effective C#、`MS` は C# 公式ドキュメント、`Analyzer` は .NET アナライザ、`Fowler` は Refactoring カタログを指す。
章構成は両書の章立てをテーマ別に統合し、全100項目を §1〜§11 に割り当てた。§12 以降はプロジェクト向けの補遺で、§12 リファクタリング指針（Fowler）、§13 モダン C#、§14 テストを収める。
コード例の `// NG` は避けるべき書き方、`// OK` は推奨する書き方を示す。
前提として `Nullable` 参照型を有効化する（`<Nullable>enable</Nullable>`）。これは TypeScript の `strict` に相当し、`null` 関連バグをコンパイル時に検出する。

---

## §1 C# 言語イディオム

- ローカル変数は型が右辺から自明なとき `var` を使う。型が読み取れない場合は明示する（EC Item 1）。
- 定数は `const` より `readonly` を優先する。`const` はコンパイル時に埋め込まれ、再ビルドしないと値が伝播しない（EC Item 2）。
- キャストより `is`／`as` 演算子を使う。失敗時の挙動が安全で、型チェックが一度で済む（EC Item 3）。
- `string.Format()` より補間文字列（`$"..."`）を使う（EC Item 4）。
- カルチャ依存の文字列整形には `FormattableString` を使い、`InvariantCulture` を明示する（EC Item 5）。
- 文字列に意味を埋め込む「文字列型 API」を避ける。`nameof` やリテラル型で置き換える（EC Item 6）。
- コールバックはデリゲート（`Action`／`Func`／`Predicate`）で表現する（EC Item 7）。
- イベント発火は null 条件演算子で行う（`Handler?.Invoke(...)`）（EC Item 8）。
- ボックス化・アンボックス化を最小化する。値型を `object` や非ジェネリックコレクションに入れない（EC Item 9）。
- `new` 修飾子（メンバー隠蔽）は基底クラスの更新に反応する目的以外で使わない（EC Item 10）。

```csharp
// NG: キャストは失敗時に例外、型チェックが二度手間
var stream = (MemoryStream)payload;
// OK: is パターンで安全に絞り込む（EC Item 3）
if (payload is MemoryStream stream) { /* stream を安全に使える */ }

// NG: 引数の対応が崩れやすい
var msg = string.Format("{0} を {1}Hz で取得", device, rate);
// OK: 補間文字列（EC Item 4）
var msg = $"{device} を {rate}Hz で取得";

// イベント発火は null 条件演算子で（EC Item 8）
DataAvailable?.Invoke(this, args);
```

---

## §2 型設計：値型・参照型・不変性・等価性

- データメンバーを公開せず、プロパティ経由で公開する（MEC Item 1）。
- 可変データには自動実装プロパティ（implicit properties）を使う（MEC Item 2）。
- 値型は不変にする。`readonly struct` で表す（MEC Item 3、EC Item 2）。
- 値型と参照型を区別して選ぶ。小さく不変で値的意味を持つなら `struct`、同一性を持つなら `class`（MEC Item 4）。
- 値型は `0`（既定値）が有効な状態になるよう設計する（MEC Item 5）。
- プロパティはデータのように振る舞わせる。重い処理や副作用を持たせない（MEC Item 6）。
- 一時的な複数戻り値は型スコープを限定するためタプルで返す（MEC Item 7）。
- 匿名型にはローカル関数を併用して扱う（MEC Item 8）。
- 等価性の複数概念（参照・値・`==`・`Equals`・`IEquatable<T>`）の関係を理解して一貫して実装する（MEC Item 9）。
- `GetHashCode()` の落とし穴を理解する。可変フィールドからハッシュを作らない（MEC Item 10）。

```csharp
// OK: 不変の値型は readonly struct（MEC Item 3, 5）。既定値 0 も有効な状態にする
public readonly struct SampleRate : IEquatable<SampleRate>
{
    public int Hz { get; }
    public SampleRate(int hz) =>
        Hz = hz >= 0 ? hz : throw new ArgumentOutOfRangeException(nameof(hz));

    public bool Equals(SampleRate other) => Hz == other.Hz;
    public override bool Equals(object? obj) => obj is SampleRate o && Equals(o);
    public override int GetHashCode() => Hz;            // 不変フィールドから算出（MEC Item 10）
}
```

---

## §3 リソース管理とオブジェクトのライフサイクル

- .NET のリソース管理（GC とファイナライザ、`IDisposable`）の仕組みを理解する（EC Item 11）。
- コンストラクタ本体の代入よりメンバー初期化子を優先する（EC Item 12）。
- 静的メンバーは静的コンストラクタまたは初期化子で正しく初期化する（EC Item 13）。
- 重複する初期化ロジックは委譲コンストラクタ（`: this(...)`）で一本化する（EC Item 14）。
- 不要なオブジェクト生成を避ける。ループ内の確保やイミュータブルな定数の再生成をしない（EC Item 15）。
- コンストラクタ内で仮想メソッドを呼ばない（EC Item 16）。
- アンマネージドリソースを持つ型は標準の Dispose パターンを実装する（EC Item 17）。

```csharp
// OK: using 宣言でスコープ終端に確実に解放（EC Item 17, 46）
using var capture = new WasapiLoopbackCapture();
using var writer = new WaveFileWriter(path, capture.WaveFormat);
// スコープを抜けると Dispose が必ず呼ばれる
```

---

## §4 ジェネリクス

- 型制約は必要十分（minimal and sufficient）に定義する（EC Item 18）。
- ジェネリックアルゴリズムは実行時型チェックで特殊化する（EC Item 19）。
- 順序関係は `IComparable<T>`／`IComparer<T>` で実装する（EC Item 20）。
- 破棄可能な型引数を扱うジェネリッククラスは、それを破棄できるよう設計する（EC Item 21）。
- 共変性・反変性（`out`／`in`）をサポートして柔軟性を上げる（EC Item 22）。
- 型引数へのメソッド制約はデリゲートで定義する（EC Item 23）。
- 基底クラスやインターフェースに対するジェネリック特殊化を作らない（EC Item 24）。
- 型引数がインスタンスフィールドにならない限り、ジェネリックメソッドを優先する（EC Item 25）。
- ジェネリックインターフェースに加えて、必要なら旧来の非ジェネリックインターフェースも実装する（EC Item 26）。
- 最小限のインターフェース契約は拡張メソッドで補う（EC Item 27）。
- 構築済みジェネリック型は拡張メソッドで機能強化する（EC Item 28）。

---

## §5 LINQ とシーケンス（関数型・遅延評価）

- コレクションを一括生成して返すより、イテレータメソッド（`yield return`）を優先する（EC Item 29）。
- ループよりクエリ構文（または同等のメソッド構文）を優先する（EC Item 30）。
- シーケンスを返す API は合成可能（composable）に設計する（EC Item 31）。
- 反復と「動作・述語・関数」を分離する（EC Item 32）。
- シーケンス要素は要求時に生成する（遅延生成）（EC Item 33）。
- 関数パラメータで結合度を下げる（EC Item 34）。
- 拡張メソッドをオーバーロードしない（EC Item 35）。
- クエリ式がメソッド呼び出しへどう変換されるかを理解する（EC Item 36）。
- クエリでは即時評価より遅延評価を優先する（EC Item 37）。
- クエリ内では名前付きメソッドよりラムダ式を優先する（EC Item 38）。
- LINQ クエリを構成するデリゲート（`Where`／`Select` 等の述語・射影）内で例外を投げない。契約違反の報告（§6 EC Item 45）とは区別する（EC Item 39）。
- 早期実行（eager）と遅延実行（deferred）を区別する（EC Item 40）。
- クロージャで高コストなリソースを長く捕捉しない（EC Item 41）。
- `IEnumerable`（LINQ to Objects）と `IQueryable`（プロバイダ変換）のデータソースを区別する（EC Item 42）。
- 単一要素の意図は `Single()`／`First()` で表明する（EC Item 43）。
- クエリ内で束縛変数を変更しない（EC Item 44）。

```csharp
// NG: コレクションを一括生成して返す（呼び出し側が全件待ち）
public List<int> Evens(IEnumerable<int> xs)
{
    var r = new List<int>();
    foreach (var x in xs) if (x % 2 == 0) r.Add(x);
    return r;
}
// OK: 遅延評価のイテレータ（EC Item 29, 37）
public IEnumerable<int> Evens(IEnumerable<int> xs) => xs.Where(x => x % 2 == 0);
```

---

## §6 例外とエラー処理

- メソッドの契約違反（事前条件違反）は例外で報告する（EC Item 45）。
- リソース解放は `using` と `try`/`finally` で確実に行う（EC Item 46）。
- アプリ固有の完全な例外クラスを用意する（意味のあるメッセージ、`innerException` による連鎖）（EC Item 47）。なお例外のシリアライズ（`ISerializable`／`SerializationInfo`）は .NET 8+ で obsolete（`SYSLIB0051`）、`BinaryFormatter` も削除済みのため実装しない（MS、確信度：高）。
- 強い例外保証（strong exception guarantee）を優先する。失敗しても状態を変更前に戻す（EC Item 48）。
- `catch` して再 `throw` するより例外フィルタ（`when`）を優先する（EC Item 49）。
- 例外フィルタの副作用（ logging 等）を活用する。ただしフィルタは純粋に近く保つ（EC Item 50）。
- 共通規約：例外は握りつぶさず、意味のあるメッセージを付けて処理する。

```csharp
// 契約違反は早期に例外で報告（EC Item 45）
if (path is null) throw new ArgumentNullException(nameof(path));

// NG: ログのためだけに catch→throw するとスタックが汚れる
try { Process(); }
catch (Exception ex) { Log(ex); throw; }
// OK: 例外フィルタで捕捉せずログ（EC Item 49, 50）
try { Process(); }
catch (Exception ex) when (LogAndContinue(ex)) { /* 到達しない */ }
```

---

## §7 API 設計

- 変換演算子（暗黙の `operator`）を公開 API に置かない（MEC Item 11）。
- メソッドのオーバーロード乱立より、省略可能引数で減らす（MEC Item 12）。
- 型の可視性は最小にする（`internal`／`private` を既定に）（MEC Item 13）。
- 継承よりインターフェースの定義・実装を優先する（MEC Item 14）。
- インターフェースメソッドと仮想メソッドの違いを理解して使い分ける（MEC Item 15）。
- 通知にはイベントパターンを実装する（MEC Item 16）。
- 内部オブジェクトへの参照を返さない（カプセル化を破らない）（MEC Item 17）。
- イベントハンドラより仮想メソッドのオーバーライドを優先する（MEC Item 18）。
- 基底クラスで定義したメソッドをオーバーロードしない（MEC Item 19）。
- イベントが実行時結合を増やすことを理解する（MEC Item 20）。
- 非仮想イベントのみを宣言する（MEC Item 21）。
- メソッドグループは明確・最小・完全にする（MEC Item 22）。
- `partial` クラスには `partial` メソッド（コンストラクタ・ミューテータ・イベントハンドラ）を与える（MEC Item 23）。
- `ICloneable` は設計の選択肢を狭めるため避ける（MEC Item 24）。
- 配列引数は `params` 配列に限定する（MEC Item 25）。

---

## §8 非同期プログラミング（Task ベース）

- イテレータ・非同期メソッドでは引数検証をローカル関数で分け、エラーを即時報告する（MEC Item 26）。
- 非同期処理には async メソッドを使う（MEC Item 27）。
- `async void` を書かない。イベントハンドラ以外は `async Task` を返す（MEC Item 28）。
- 同期メソッドと非同期メソッドを混在合成しない（`.Result`／`.Wait()` でブロックしない）（MEC Item 29）。
- スレッド確保とコンテキストスイッチを避けるため async を使う（MEC Item 30）。
- ライブラリ層では不要な同期コンテキスト復帰を避ける（`ConfigureAwait(false)`）（MEC Item 31）。
- 非同期処理は `Task` オブジェクトで合成する（`WhenAll`／`WhenAny`）（MEC Item 32）。
- キャンセルプロトコル（`CancellationToken`）の実装を検討する（MEC Item 33）。
- 汎用の非同期戻り値型（`ValueTask`／キャッシュ済み `Task`）でアロケーションを抑える（MEC Item 34）。

```csharp
// NG: イベントハンドラを兼ねる async void の本体で例外が起きると、発火元で捕捉できずプロセスを落とす（MEC Item 28）
async void OnData(object? s, WaveInEventArgs e) => await UploadAsync(e.Buffer);
// OK: イベントハンドラは async void が許される唯一の例外。薄く保ち try/catch で例外を閉じ込め、実処理は async Task へ委譲する（MEC Item 28）
async void OnData(object? s, WaveInEventArgs e)
{
    try { await OnDataAsync(e.Buffer); }
    catch (Exception ex) { _log.LogError(ex, "音声アップロードに失敗"); }
}
async Task OnDataAsync(byte[] buffer) => await UploadAsync(buffer);

// ライブラリ層では同期コンテキストへ戻さない（MEC Item 31）
var result = await transcribe.SendAsync(chunk).ConfigureAwait(false);
```

---

## §9 並列処理と同期

- PLINQ が並列アルゴリズムをどう実装するかを理解する（MEC Item 35）。
- 並列アルゴリズムは例外（`AggregateException`）を前提に組む（MEC Item 36）。
- スレッドを自前生成せずスレッドプールを使う（MEC Item 37）。
- UI 跨ぎの通信には `BackgroundWorker`（または同等の仕組み）を使う（MEC Item 38）。※レガシー。本プロジェクトはコンソール実行体で UI を持たず適用外。現代は `async`/`await`＋`IProgress<T>` を使う（MS）。
- XAML 環境のクロススレッド呼び出し（ディスパッチャ）を理解する（MEC Item 39）。※同上、XAML/UI 非使用のため適用外。
- 同期の第一選択は `lock()` にする（MEC Item 40）。
- ロックのスコープは可能な限り小さくする（MEC Item 41）。
- ロック保持中に未知のコード（コールバック）を呼ばない（MEC Item 42）。

```csharp
// NG: ロック中に未知のハンドラを呼ぶ（MEC Item 42）— デッドロックの温床
lock (_gate) { _handler?.Invoke(buffer); }
// OK: ハンドラ参照だけロック内で取得し、呼び出しはロック外（MEC Item 41, 42）
EventHandler<Buffer>? handler;
lock (_gate) { handler = _handler; }
handler?.Invoke(this, buffer);
```

---

## §10 動的プログラミング

- 動的型付け（`dynamic`）の利点と欠点を理解し、限定的に使う（MEC Item 43）。
- ジェネリック型引数の実行時型を活かす用途に `dynamic` を使う（MEC Item 44）。
- データ駆動の動的型には `DynamicObject`／`IDynamicMetaObjectProvider` を使う（MEC Item 45）。
- 式ツリー API（`Expression`）の使い方を理解する（MEC Item 46）。
- 公開 API では `dynamic` を最小化する。境界では静的型に変換する（MEC Item 47）。

---

## §11 整形・命名・静的解析

- 整形は `.editorconfig` と `dotnet format` に従い、整形ルールを個別に議論しない（MS・Analyzer）。
- .NET アナライザ（Roslyn）を有効化し、警告に従う。抑制する場合は理由をコメントで添える（Analyzer・MEC Item 50）。
- 命名は MS 公式の規約に従う（MS）。
  - 型・メソッド・プロパティ・イベント・名前空間・`public` フィールド・定数・enum メンバー：`PascalCase`
  - ローカル変数・メソッド引数：`camelCase`
  - `private`／`internal` インスタンスフィールド：`_camelCase`（先頭アンダースコア）
  - `private static` フィールド：`s_camelCase`、`[ThreadStatic]`：`t_camelCase`
  - インターフェースは `I` 接頭辞（`IDisposable`）、型パラメータは `T` 接頭辞（`TResult`）
  - 非同期メソッドは `Async` 接尾辞（`ReadAsync`）
- 公開要素の命名は問題領域の語彙に合わせる（MS・Framework Design Guidelines）。
- 最も人気の回答ではなく最善の回答を選ぶ。仕様とコードに参加し、慣行はアナライザで自動化する（MEC Item 48, 49, 50）。

```csharp
// 命名規約の例（MS）
public sealed class AudioCapture          // 型は PascalCase
{
    private readonly int _sampleRate;     // private フィールドは _camelCase
    private static readonly object s_gate = new();  // private static は s_

    public async Task<int> ReadAsync(byte[] buffer) // 非同期は Async 接尾辞
    {
        var bytesRead = buffer.Length;    // ローカルは camelCase
        return await Task.FromResult(bytesRead).ConfigureAwait(false);
    }
}
```

---

## §12 リファクタリング指針（関数型志向と最適化）

> 既定は LINQ と不変データで宣言的に書く。手続き的な最適化は、計測でホットスポットと判明した箇所に限る。出典：Martin Fowler『Refactoring』カタログ（<https://refactoring.com/catalog/>）。

### 関数型で書く（明示的ループを避ける）

- `for`／`foreach` は、まず LINQ（`Where`・`Select`・`Aggregate`・`Any`・`All`・`SelectMany`）で置き換えられないか検討する（EC Item 30、Fowler "Replace Loop with Pipeline"）。
- 条件分岐は早期リターン、または型による分岐（`switch` 式とパターンマッチング）で整理する（EC Item 30 / MS）。
- 破壊的変更を避け、`Select`／`Where`／コレクション式で新しい値を作る（共通規約：イミュータビリティ、`readonly`／`record`／不変コレクション）。
- 副作用のみの繰り返しには `foreach` を使う。変換・集約に `List.ForEach` を使わない（早期 `return` で中断できないため）。

```csharp
// NG: 明示ループ＋可変状態
var total = 0m;
foreach (var o in orders) if (o.Paid) total += o.Amount;
// OK: パイプライン（EC Item 30 / Fowler Replace Loop with Pipeline）
var total = orders.Where(o => o.Paid).Sum(o => o.Amount);
```

### ネストは2段まで（早期リターン・関数抽出）

- `if`／`for`／`foreach` のネストは2段までにする。3段以上になったら早期リターンか関数抽出で平坦化する（Fowler "Replace Nested Conditional with Guard Clauses" / "Extract Function"）。
- ガード節で前提を先に弾き、本体のインデントを下げる。null 条件演算子（`?.`）とパターンマッチングで深い分岐を畳む。

```csharp
// NG: ネスト3段
decimal Process(Item? x)
{
    if (x is not null) { if (x.Valid) { if (x.Amount > 0) { return x.Amount; } } }
    throw new ArgumentException("invalid");
}
// OK: ガード節＋パターンで平坦化
decimal Process(Item? x)
{
    if (x is not { Valid: true, Amount: > 0 }) throw new ArgumentException("invalid");
    return x.Amount;
}
```

### ループ最適化（計測が前提）

- まず計測する。BenchmarkDotNet や `dotnet-trace`／プロファイラで遅いと確認した箇所だけ最適化する。
- ループ不変式（毎回同じ計算・プロパティ解決）はループ外のローカル変数に巻き上げる（EC Item 15）。
- ホットパスでは LINQ の連鎖が中間アロケーションを生む。計測で問題が出た箇所のみ、`foreach` や `Span<T>`／`ArrayPool<T>` に置き換えて確保を消す（確信度：中）。
- 要素数が分かるなら `new List<T>(capacity)` で事前確保し、再確保を避ける。
- ボックス化を避ける（EC Item 9）。ホットパスでジェネリックや値型を保つ。

```csharp
// NG: ループ不変なプロパティ解決を毎回行う
foreach (var row in rows) output.Add(row.Value * config.Rate.Factor());
// OK: 不変式を巻き上げる（EC Item 15）
var factor = config.Rate.Factor();   // ループ不変式の巻き上げ
foreach (var row in rows) output.Add(row.Value * factor);
```

### 事前確保でアロケーションを削る（容量が分かるとき）

> 最終的な要素数や長さが事前に分かるなら、初期容量を指定して内部バッファの再確保とコピーを避ける。.NET では確保が GC 圧に直結するため効果が大きい（EC Item 15）。ただし過剰確保はメモリの無駄になるため、容量は実測値か妥当な上限に基づく。まず計測する（§12 冒頭）。

- 要素数が既知のコレクションは初期容量を渡す。`List<T>`／`Dictionary<TKey,TValue>`／`HashSet<T>` はいずれもコンストラクタで容量を受け取る（EC Item 15）。
- 既存の `List<T>` に大量追加する前は `EnsureCapacity(n)`（.NET 6+）で一度だけ確保する。
- 要素数が完全に確定し可変長が不要なら、`List<T>` ではなく配列 `new T[n]` を直接使う。
- 最終長が概算できる文字列構築は `StringBuilder` に初期容量を渡す。`+=` の連結ループは避ける。
- 短命な一時バッファはプールから借りて再利用する。`ArrayPool<T>.Shared.Rent(n)` で借り、`try`/`finally` で必ず `Return` する。音声バッファ（PCM フレーム）のように毎フレーム確保する経路で GC 圧を大きく下げられる。
- 小さく短命なバッファはヒープを使わず `stackalloc`（`Span<T>`）でスタックに確保する。サイズ上限（目安：1KB 未満）に注意し、ループ内で `stackalloc` しない。
- 既存配列・バッファの部分参照は `Span<T>`／`Memory<T>` のスライスで取り、部分コピーを避ける（EC Item 15）。
- 高度：事前確保した `List<T>` へ直接書き込むなら `CollectionsMarshal.AsSpan`（.NET 5+）／`CollectionsMarshal.SetCount`（.NET 8+）で `Add` のたびの境界チェックを避けられる（確信度：中、要計測）。

```csharp
// NG: 容量未指定だと内部配列が 4→8→16… と再確保・コピーを繰り返す
var samples = new List<short>();
for (var i = 0; i < frameCount; i++) samples.Add(Read());

// OK: 要素数が分かるので初期容量を確保（EC Item 15）
var samples = new List<short>(frameCount);
for (var i = 0; i < frameCount; i++) samples.Add(Read());

// OK: 毎フレームの一時バッファはプールから借りて使い回す（GC 圧の削減）
var buffer = ArrayPool<byte>.Shared.Rent(byteCount);
try
{
    var span = buffer.AsSpan(0, byteCount);
    FillPcm(span);
    Process(span);
}
finally
{
    ArrayPool<byte>.Shared.Return(buffer);
}

// OK: 小さく短命なバッファはスタックに確保（ヒープ確保ゼロ）
Span<byte> header = stackalloc byte[44]; // WAV ヘッダ
WriteHeader(header);
```

---

## §13 モダン C#（規範書を補完する言語機能）

> 規範書 2 冊は C# 6.0／7.0 対応（2017 年刊）で、現時点でこれ以上新しい版は無い。本プロジェクトは `net10.0-windows`／`LangVersion=latest`（C# 14 相当）で約 8 世代新しいため、書籍が古びた箇所を現行機能で補正する。イミュータビリティ・型設計・非同期の具体方針は `.claude/rules/csharp/coding-style.md`・`patterns.md` に従い、本節は書籍項目との対応と本プロジェクトでの用途に絞る（重複を避ける）。

### 型・不変性（§2・§3 を補完）

- 不変の値的モデルは、手書きの `readonly struct`＋`IEquatable<T>`＋`GetHashCode` より `record`／`readonly record struct` を優先する。値等価・`with` 式・分解・`ToString` が自動生成される（§2 の MEC Item 3・9・10 を大きく置き換える。MS）。
- 不変な構築は `init` セッターと `required` メンバーで表し、巨大なコンストラクタや委譲コンストラクタ（EC Item 14）の多くを不要にする（MS、rules/csharp/patterns.md の Options パターン）。
- コンストラクタの定型はプライマリコンストラクタ（C# 12）で圧縮する（MS）。

### パターンマッチング（§1・§12 を補完）

- 型・構造による分岐は `switch` 式とプロパティ／リストパターンで宣言的に書く。`is`／`as`（EC Item 3）の後段や §12 のガード節をより簡潔にできる（MS）。

### 非同期ストリームと確定的解放（§5・§8 を補完）

- 逐次生成される非同期シーケンス（Transcribe Streaming の途中結果など）は `IAsyncEnumerable<T>`＋`await foreach` で表す。イテレータ（EC Item 29）の非同期版にあたる（MS）。列挙にキャンセルを通すため `[EnumeratorCancellation]` を付ける。
- 非同期に解放すべき資源は `IAsyncDisposable`＋`await using` で確実に解放する（EC Item 46・§6 の非同期版。MS）。

### 生産者-消費者とキャンセル（§9 を補完）

- スレッド／タスク間のストリーミング受け渡しは、自前の `lock`＋キュー（§9 MEC Item 40-42）より `System.Threading.Channels`（`Channel<T>`）を優先する。
- 公開非同期 API は `CancellationToken` を受け取り末端まで伝播する（MEC Item 33、rules/csharp/coding-style.md）。

### コレクション式（§12 を補完）

- 配列・リスト等の初期化はコレクション式 `[a, b, c]`、連結はスプレッド `[.. xs, y]` で簡潔に書く（C# 12。MS）。

```csharp
// OK: 不変の値型は readonly record struct（値等価・with・分解が自動。§2 の手書き実装を置換）
public readonly record struct SampleRate(int Hz);   // default(SampleRate) は Hz=0（MEC Item 5）

// OK: Transcribe Streaming の途中結果を非同期ストリームで返す（IAsyncEnumerable＋キャンセル伝播）
public async IAsyncEnumerable<Caption> ReadCaptionsAsync(
    [EnumeratorCancellation] CancellationToken ct)
{
    await foreach (var seg in _segments.WithCancellation(ct))
        yield return new Caption(seg.SpeakerId, seg.Text);
}
```

---

## §14 テスト

> テストの詳細は `.claude/rules/csharp/testing.md`（および `common/testing.md`）に従う。本節は要点と本プロジェクトの構成を示す。TDD（RED → GREEN → IMPROVE）とカバレッジ 80% 以上は共通規約。

- フレームワークは **xUnit**（`[Fact]`／`[Theory]`）。アサーションは **FluentAssertions**、依存のモックは **Moq** または **NSubstitute** を使う（rules/csharp/testing.md。実依存: `xunit` 2.9.3・`coverlet.collector`）。
- テストは振る舞いで命名し、実装詳細で命名しない（例: `ReadCaptions_StopsEnumeration_WhenCancelled`）。本体は AAA（Arrange-Act-Assert）で構成する（common/testing.md）。
- `tests/` は `src/` 構成をミラーする。
- カバレッジは `dotnet test --collect:"XPlat Code Coverage"` で収集し（coverlet）、ドメインロジック・入力検証・失敗経路を重点的に覆う。閾値 80% を CI で確認する。
- 非同期・キャンセル・例外の各経路を正常系と同等に検証する。契約違反（§6 EC Item 45）とキャンセル（§13・MEC Item 33）は特に重要。
- 本プロジェクトの C# ユニットはコンソール実行体（capture／recorder／live）であり ASP.NET Core ではない。rules 内の `WebApplicationFactory`／Testcontainers の記述は該当プロジェクトでのみ適用する。

```csharp
// AAA＋xUnit＋FluentAssertions（振る舞いで命名）
[Fact]
public void With_ProducesValueEqualInstance_ForSameHz()
{
    // Arrange
    var rate = new SampleRate(16_000);
    // Act
    var same = rate with { Hz = 16_000 };
    // Assert
    same.Should().Be(rate);   // record の値等価（§13）
}
```
