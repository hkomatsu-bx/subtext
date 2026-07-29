---
type: reference
title: "Python プログラミングガイド"
description: "Python のコーディング規約（Effective Python / PEP / ruff・mypy 準拠）"
---

# Python プログラミングガイド

Python のコーディング規約。本プロジェクトで Python コードを書く際は本ガイドに従う。

## 出典と優先順位

複数の規約が衝突する場合、上にあるものを優先する。

1. **参考書『Effective Python: 125 Specific Ways to Write Better Python』第3版**（Brett Slatkin 著, Addison-Wesley, 2024）。本ガイドの項目番号・原題はこれに準拠し、根拠としては公開された項目タイトルと要点のみを引用する（本文の逐語転載はしない）。本書を `EP` と略す
2. **Python 公式ドキュメントと PEP**（<https://docs.python.org/3/>）— 言語仕様・公式の規範。とりわけ PEP 8（スタイル）、PEP 20（The Zen of Python）、PEP 257（docstring）、`typing`（型ヒント）。本書群を `PEP` と略す
3. **ruff と mypy**（<https://docs.astral.sh/ruff/>、<https://mypy.readthedocs.io/>）— 静的解析・機械的強制レイヤー。「何を禁止し、何を自動修正するか」の根拠。`ruff` は Lint と整形（Black 互換）、`mypy` は型検査を担う
4. プロジェクト共通規約（`.claude/rules/` の common・python）およびユーザーグローバル規約

> **例外（プロジェクト固有の決定を優先）**: この順位は設計判断・イディオムに適用する。ただし **Python バージョン・テストフレームワーク・依存ライブラリ・ツールチェーンなどプロジェクト固有の決定は、4 のプロジェクト規約が 1〜3（書籍・公式規範・ツール既定）に優先する**。例: 本プロジェクトの Python は 3.13 固定（`.python-version`／`requires-python`）、テストは pytest（`.claude/rules/python/testing.md`）であり、書籍が別のバージョンやフレームワークを前提にしていてもプロジェクトの決定に従う。

### 補助リファレンス（規範ではなく参考）

設計の裏取りや実装の参考に用いる。規約の根拠としては 1〜4 を優先する。

- **『Fluent Python』第2版／第3版**（Luciano Ramalho 著, O'Reilly）：データモデル・イテレータ・並行処理の網羅的な深掘り。設計の裏取りに用いる
- **Refactoring カタログ（Martin Fowler）**（<https://refactoring.com/catalog/>）：リファクタリング手法の定番カタログ（§15 の根拠）

> 設計判断（Pythonic な書き方・関数設計・クラス設計・並行処理・堅牢性）は必ず Effective Python の該当項目を根拠にする。言語仕様・スタイルは PEP に従い、整形と機械的強制は ruff、型検査は mypy で行う。補助リファレンスは規範ではなく参考にとどめる。

---

## 規約の読み方

各項目は「規約（命令形）＋ 根拠（`EP Item N` は Effective Python 第3版の項目番号）」で記す。
`EP` は Effective Python、`PEP` は Python 公式ドキュメント／PEP、`ruff` は ruff、`mypy` は mypy を指す。
章構成は Effective Python 第3版の全14章（125項目）に準拠する。
コード例の `# NG` は避けるべき書き方、`# OK` は推奨する書き方を示す。
前提として型ヒントを付け、`ruff`（Lint・整形）と `mypy`（型検査、`strict` 推奨）を有効にする。これは TypeScript の `strict` ＋ eslint に相当する。
Python バージョンはプロジェクトローカル（uv）に固定する。本プロジェクトは **3.13**（`.python-version`＝`uv python pin`、`requires-python = ">=3.13"`。ruff `target-version = "py313"`）。なお 3.13 のフリースレッド（no-GIL）ビルドは 2026 年時点でも experimental のため通常ビルドを使う（確信度：高）。

---

## §1 Pythonic な思考

- 使っている Python のバージョンを把握する。本プロジェクトは 3.13（EP Item 1）。
- PEP 8 スタイルガイドに従う（EP Item 2、PEP・ruff）。
- Python はコンパイル時にエラーを検出しないと理解する。型検査（mypy）とテストで補う（EP Item 3）。
- 複雑な式よりヘルパー関数を書く（EP Item 4）。
- インデックスより多重代入アンパックを使う（EP Item 5）。
- 単一要素タプルは必ず括弧で囲む（EP Item 6）。
- 単純なインライン論理には条件式（三項演算子）を検討する（EP Item 7）。
- 代入式（ウォルラス演算子 `:=`）で繰り返しを防ぐ（EP Item 8）。
- フロー制御の分解には `match` を検討する。`if` で十分なら使わない（EP Item 9）。

```python
# NG: インデックスで取り出すと意味が読み取れない
sr, ch = config[0], config[1]
# OK: 多重代入アンパック（EP Item 5）
sample_rate, channels = config

# 代入式で重複を防ぐ（EP Item 8）
if (n := len(buffer)) > THRESHOLD:
    process(buffer, n)
```

---

## §2 文字列とスライス

- `bytes` と `str` の違いを理解し、境界で明示的に変換する（EP Item 10）。
- C 形式や `str.format` より補間 f-string を優先する（EP Item 11）。
- オブジェクト表示では `repr` と `str` の違いを理解する（EP Item 12）。
- 暗黙の文字列連結より明示的な連結を優先する（特にリスト内）（EP Item 13）。
- シーケンスのスライス方法を理解する（EP Item 14）。
- 1つの式でストライドとスライスを併用しない（EP Item 15）。
- スライスよりキャッチオールアンパック（`*rest`）を優先する（EP Item 16）。

```python
# NG: C 形式の書式指定は対応が崩れやすい
msg = "%s を %dHz で取得" % (device, rate)
# OK: 補間 f-string（EP Item 11）
msg = f"{device} を {rate}Hz で取得"

# 音声データはバイト列。境界で str と区別する（EP Item 10）
def to_wav_header(pcm: bytes) -> bytes: ...
```

---

## §3 ループと反復子

- `range` より `enumerate` を優先する（EP Item 17）。
- 並行して処理する反復子は `zip` で扱う（EP Item 18）。
- `for`／`while` の後の `else` ブロックを避ける（EP Item 19）。
- ループ変数をループ終了後に使わない（EP Item 20）。
- 引数の反復は防御的に行う（イテレータは一度しか回せない）（EP Item 21）。
- 反復中にコンテナを変更しない。コピーやキャッシュを使う（EP Item 22）。
- 短絡評価には `any`／`all` に反復子を渡す（EP Item 23）。
- 反復子・ジェネレータ操作には `itertools` を検討する（EP Item 24）。

---

## §4 辞書

- 辞書の挿入順序への依存に注意する（EP Item 25）。
- キー欠損処理は `in`／`KeyError` より `get` を優先する（EP Item 26）。
- 内部状態の欠損処理は `setdefault` より `defaultdict` を優先する（EP Item 27）。
- キー依存の既定値は `__missing__` で構築する（EP Item 28）。
- 辞書・リスト・タプルの深い入れ子より、クラス合成を選ぶ（EP Item 29）。

```python
# NG: 深い入れ子は読みにくく壊れやすい
speakers = {"spk_0": {"name": "田中", "segments": [(0.0, 3.2)]}}
# OK: dataclass で合成（EP Item 29, 51）
@dataclass
class Speaker:
    name: str
    segments: list[tuple[float, float]]
```

---

## §5 関数

- 関数に渡した引数は関数内で変更されうる（参照渡し）と理解し、呼び出し側の値を保持したいなら防御的にコピーする（EP Item 30）。
- 3つを超える戻り値は専用の結果オブジェクト（dataclass・NamedTuple）で返す（EP Item 31）。
- `None` を返すより例外を送出する（EP Item 32）。
- クロージャと変数スコープ、`nonlocal` の関係を理解する（EP Item 33）。
- 可変長位置引数（`*args`）で視覚的ノイズを減らす（EP Item 34）。
- 任意の挙動はキーワード引数で提供する（EP Item 35）。
- 動的なデフォルト引数は `None` と docstring で指定する（EP Item 36）。
- キーワード専用・位置専用引数で呼び出しの明確さを強制する（EP Item 37）。
- 関数デコレータは `functools.wraps` で定義する（EP Item 38）。
- グルーコードには `lambda` より `functools.partial` を優先する（EP Item 39）。

```python
# NG: ミュータブルなデフォルト引数は全呼び出しで共有される（EP Item 36）
def append(item, into=[]): into.append(item); return into
# OK: None センチネルと docstring（EP Item 36）
def append(item, into: list | None = None) -> list:
    """into が None なら新しいリストを生成する。"""
    if into is None:
        into = []
    into.append(item)
    return into
```

---

## §6 内包表記とジェネレータ

- `map`／`filter` より内包表記を使う（EP Item 40）。
- 内包表記の制御部分式（`for`・`if`）は2つまでにする（EP Item 41）。
- 内包表記の繰り返しは代入式で減らす（EP Item 42）。
- リストを返すよりジェネレータ（`yield`）を検討する（EP Item 43）。
- 大きなリスト内包にはジェネレータ式を検討する（EP Item 44）。
- 複数ジェネレータは `yield from` で合成する（EP Item 45）。
- `send` メソッドより、反復子を引数で渡す（EP Item 46）。
- `throw` メソッドより、状態遷移はクラスで管理する（EP Item 47）。

```python
# NG: 大きな入力でリストを一括生成（メモリを食う）
def chunks(data): return [data[i:i+N] for i in range(0, len(data), N)]
# OK: ジェネレータで遅延生成（EP Item 43, 44）
def chunks(data):
    for i in range(0, len(data), N):
        yield data[i:i+N]
```

---

## §7 クラスとインターフェース

- 単純なインターフェースはクラスより関数を受け取る（EP Item 48）。
- `isinstance` 分岐よりオブジェクト指向の多態を優先する（EP Item 49）。
- 関数型スタイルには `functools.singledispatch` を検討する（EP Item 50）。
- 軽量クラスは `dataclasses` で定義する（EP Item 51）。
- 汎用的なオブジェクト生成には `@classmethod` 多態を使う（EP Item 52）。
- 親クラスは `super` で初期化する（EP Item 53）。
- 機能合成には Mix-in クラスを検討する（EP Item 54）。
- プライベート属性よりパブリック属性を優先する（EP Item 55）。
- 不変オブジェクトは `dataclasses`（`frozen=True`）で作る（EP Item 56）。
- カスタムコンテナ型は `collections.abc` を継承する（EP Item 57）。

---

## §8 メタクラスと属性

- セッター／ゲッターメソッドより素の属性を使う（EP Item 58）。
- 属性のリファクタリングには `@property` を検討する（EP Item 59）。
- 再利用可能な `@property` にはディスクリプタを使う（EP Item 60）。
- 遅延属性には `__getattr__`・`__getattribute__`・`__setattr__` を使う（EP Item 61）。
- サブクラスの検証は `__init_subclass__` で行う（EP Item 62）。
- クラス存在の登録は `__init_subclass__` で行う（EP Item 63）。
- クラス属性の注釈には `__set_name__` を使う（EP Item 64）。
- 属性間の関係はクラス本体の定義順序で確立する（EP Item 65）。
- 合成可能なクラス拡張は、メタクラスよりクラスデコレータを優先する（EP Item 66）。

---

## §9 並行処理と並列処理

- 子プロセスの管理には `subprocess` を使う（EP Item 67）。
- ブロッキング I/O にはスレッドを使い、並列計算には使わない（GIL のため）（EP Item 68）。
- スレッド間のデータ競合は `Lock` で防ぐ（EP Item 69）。
- スレッド間の作業調整は `Queue` で行う（EP Item 70）。
- 並行処理が本当に必要かを見極める（EP Item 71）。
- オンデマンドのファンアウトに新規スレッドを毎回生成しない（EP Item 72）。
- `Queue` による並行化はリファクタリングを要すると理解する（EP Item 73）。
- スレッドが必要なら `ThreadPoolExecutor` を検討する（EP Item 74）。
- 高並行な I/O はコルーチン（`async`／`await`）で実現する（EP Item 75）。
- スレッド I/O を `asyncio` へ移植する方法を知る（EP Item 76）。
- 移行を容易にするためスレッドとコルーチンを混在させる（EP Item 77）。
- async 対応のワーカースレッドで `asyncio` イベントループの応答性を保つ（EP Item 78）。
- 真の並列性には `concurrent.futures` を検討する（EP Item 79）。

---

## §10 堅牢性

- `try`／`except`／`else`／`finally` の各ブロックを活用する（EP Item 80）。
- 内部前提は `assert` で表明し、想定外の入力は例外を送出する（EP Item 81）。
- 再利用可能な `try`／`finally` には `contextlib` と `with` を検討する（EP Item 82）。
- `try` ブロックは可能な限り短くする（EP Item 83）。
- 例外変数が `except` ブロック外で消えることに注意する（EP Item 84）。
- `Exception` クラスの捕捉に注意する（広すぎる `except` を避ける）（EP Item 85）。
- `Exception` と `BaseException` の違いを理解する（EP Item 86）。
- 詳細な例外報告には `traceback` を使う（EP Item 87）。
- トレースバックを明確にするため例外を明示的に連鎖する（`raise ... from`）（EP Item 88）。
- リソースは常にジェネレータの外から渡し、呼び出し側で解放する（EP Item 89）。
- `__debug__` を `False` にしない（EP Item 90）。
- 開発ツールを作る場合を除き `exec`／`eval` を使わない（EP Item 91）。
- 共通規約：例外は握りつぶさず、意味のあるメッセージを付けて処理する。

```python
# NG: 元の例外情報が失われる
try:
    transcribe(path)
except ClientError:
    raise RuntimeError("文字起こし失敗")
# OK: 例外連鎖で原因を保持する（EP Item 88）
try:
    transcribe(path)
except ClientError as e:
    raise RuntimeError(f"文字起こし失敗: {path}") from e
```

---

## §11 パフォーマンス

- 最適化の前にプロファイルする（`cProfile`）（EP Item 92）。
- 性能要所は `timeit` のマイクロベンチで最適化する（EP Item 93）。
- Python を別言語に置き換える時期と方法を知る（EP Item 94）。
- ネイティブライブラリとの迅速な統合には `ctypes` を検討する（EP Item 95）。
- 性能と使い勝手の両立には拡張モジュール（C 拡張）を検討する（EP Item 96）。
- 起動時間の改善にはバイトコードキャッシュとファイルシステムキャッシュに頼る（EP Item 97）。
- 起動時間の短縮には動的インポートでモジュールを遅延読み込みする（EP Item 98）。
- `bytes` とのゼロコピー操作には `memoryview` と `bytearray` を検討する（EP Item 99）。

---

## §12 データ構造とアルゴリズム

- 複雑な基準のソートは `key` パラメータで行う（EP Item 100）。
- `list.sort`（破壊的）と `sorted`（新規生成）の違いを理解する（EP Item 101）。
- ソート済みシーケンスの探索には `bisect` を検討する（EP Item 102）。
- 生産者-消費者キューには `deque` を優先する（EP Item 103）。
- 優先度付きキューには `heapq` を使う（EP Item 104）。
- ローカル時計には `time` より `datetime` を使う（EP Item 105）。
- 精度が最重要なら `decimal` を使う（EP Item 106）。
- `pickle` のシリアライズは `copyreg` で保守可能にする（EP Item 107）。

---

## §13 テストとデバッグ

> 本プロジェクトのテストフレームワークは **pytest**（`pytest`／`pytest-cov`。`.claude/rules/python/testing.md`）。以下の EP 項目は unittest を前提に書かれているが、**原則は普遍で手段を pytest に読み替える**（モックは `unittest.mock` か `pytest-mock` の `mocker`、カバレッジは `pytest --cov=src`）。

- 関連する振る舞いをまとめて検証する（EP Item 108。unittest の `TestCase` サブクラスに相当。pytest ではテスト関数＋`assert`）。
- ユニットテストより統合テストを優先する（EP Item 109）。
- テストは互いに隔離する（EP Item 110。unittest の `setUp`／`tearDown` は pytest の `@pytest.fixture` で表す）。
- 複雑な依存（AWS API など）のテストにはモックを使う（EP Item 111）。
- モック・テストを容易にするため依存をカプセル化する（EP Item 112）。
- 浮動小数点の比較は許容誤差付きで行う（EP Item 113。unittest の `assertAlmostEqual` は pytest の `pytest.approx`）。
- 対話的デバッグには `pdb` を検討する（EP Item 114）。
- メモリ使用・リークの把握には `tracemalloc` を使う（EP Item 115）。
- 共通規約：カバレッジ 80% 以上、TDD（RED → GREEN → IMPROVE）を守る。

---

## §14 協働

- コミュニティ製モジュールの探し方を知る（PyPI）（EP Item 116）。
- 隔離・再現可能な依存には仮想環境を使う（EP Item 117）。本プロジェクトは uv で管理し、グローバル環境を汚さない（PEP・共通規約）。
- すべての関数・クラス・モジュールに docstring を書く（EP Item 118、PEP 257）。
- モジュールの整理と安定 API 提供にはパッケージを使う（EP Item 119）。
- デプロイ環境の設定にはモジュールスコープのコードを検討する（EP Item 120）。
- 呼び出し側を API から隔離するためルート例外を定義する（EP Item 121）。
- 循環依存の解消方法を知る（EP Item 122）。
- 利用箇所の移行・リファクタリングには `warnings` を検討する（EP Item 123）。
- バグを未然に防ぐため `typing` による静的解析を行う（EP Item 124、mypy）。
- Python プログラムの同梱は `zipimport`／`zipapp` よりオープンソースのツールを優先する（EP Item 125）。

---

## §15 リファクタリング指針（関数型志向と最適化）

> 既定は内包表記・ジェネレータ・不変データで宣言的に書く。手続き的な最適化は、計測でホットスポットと判明した箇所に限る。出典：Martin Fowler『Refactoring』カタログ（<https://refactoring.com/catalog/>）。

### 関数型で書く（明示的ループを避ける）

- 明示的な `for` ループは、まず内包表記・ジェネレータ式・`sum`／`any`／`all`・`itertools` で置き換えられないか検討する（EP Item 24, 40、Fowler "Replace Loop with Pipeline"）。`functools.reduce` は可読性を下げやすいため最後の手段とする。
- 条件分岐は早期リターン、または `match` 文（構造的パターンマッチング）で整理する（§1・§10 / EP Item 9）。
- 破壊的変更を避け、新しいリスト・タプル・`frozen` dataclass を作る（共通規約：イミュータビリティ）。
- 副作用のみの繰り返しには通常の `for` を使う。変換・集約には内包表記やジェネレータを使う。

```python
# NG: 明示ループ＋可変状態
total = 0
for o in orders:
    if o.paid:
        total += o.amount
# OK: ジェネレータ式＋sum（EP Item 40 / Fowler Replace Loop with Pipeline）
total = sum(o.amount for o in orders if o.paid)
```

### ネストは2段まで（早期リターン・関数抽出）

- `if`／`for`／`while` のネストは2段までにする。3段以上になったら早期リターンか関数抽出で平坦化する（EP Item 4、Fowler "Replace Nested Conditional with Guard Clauses" / "Extract Function"）。
- ruff の複雑度・ネスト関連ルール（`C901` など）で機械的に監視する（ruff）。
- ガード節で前提を先に弾き、本体のインデントを下げる。

```python
# NG: ネスト3段
def process(x):
    if x is not None:
        if x.valid:
            if x.amount > 0:
                return x.amount
    raise ValueError("invalid")
# OK: ガード節で平坦化（EP Item 4, 32）
def process(x):
    if x is None or not x.valid or x.amount <= 0:
        raise ValueError("invalid")
    return x.amount
```

### ループ最適化（計測が前提）

- まず計測する。`cProfile` でホットスポットを特定し、`timeit` で前後を比較してから最適化する（EP Item 92, 93）。
- ループ不変式（毎回同じ計算・属性解決）はループ外のローカル変数に巻き上げる。
- ホットパスではグローバル・属性アクセスをローカル変数に束縛して解決コストを下げる（確信度：中）。
- 大量データは中間リストを作らずジェネレータで流す（EP Item 43, 44）。
- `bytes` の大規模操作は `memoryview`／`bytearray` でコピーを避ける（EP Item 99）。

```python
# NG: ループ不変な属性解決を毎回行う
out = []
for row in rows:
    out.append(row.value * config.rate.factor())
# OK: 不変式を巻き上げる
factor = config.rate.factor()   # ループ不変式の巻き上げ
out = [row.value * factor for row in rows]
```

### 事前確保は言語特性を踏まえて（多くの場合 list では不要）

> 「サイズが分かるなら先に確保する」は C 系言語の定石だが、Python の `list` ではほぼ効かない。CPython の `list` は追加時に内部配列を指数的に余剰確保するため、`[None] * n` で事前確保しても再確保の削減効果は小さく、可読性だけ落ちる。事前確保が本当に効くのは固定幅のバイナリ／数値バッファに限られる。まず計測する（EP Item 92）。

- `list` をインデックス代入のためだけに `[None] * n` で事前確保しない。内包表記で一括構築する方が速く読みやすい（EP Item 40）。要素数が分かっても通常は内包表記で十分。
- 固定長のバイナリバッファは `bytearray(n)` で確保し、スライス代入や `memoryview` で書き込む。音声 PCM バッファのように長さが既知で連続したバイト列に好適（EP Item 99）。
- 同種の数値の連続バッファは `array.array(typecode, ...)` を使う。`list` よりメモリが密で、要素数が大きいほど有利。
- 数値計算で要素数が確定しているなら `numpy.empty(n)`／`numpy.zeros(n)` で事前確保する（依存追加の是非は別途判断）。
- 固定長の履歴・リングバッファは `collections.deque(maxlen=n)` を使う。古い要素が自動で押し出され、確保が一定に保たれる。
- `dict`／`set` に容量指定の公開 API はない。事前確保するより、内包表記で一括構築してリハッシュ回数を減らす（EP Item 40）。
- 大量データは事前確保せずジェネレータで流し、そもそも全体を確保しない選択を優先する（EP Item 43, 44）。

```python
# NG: list を「サイズ分」事前確保してインデックス代入（Python では効果が薄く読みにくい）
out = [None] * frame_count
for i in range(frame_count):
    out[i] = read()
# OK: 内包表記で一括構築（EP Item 40）
out = [read() for _ in range(frame_count)]

# OK: 固定長バイナリは bytearray で事前確保し、ゼロコピーで書き込む（EP Item 99）
pcm = bytearray(byte_count)          # 長さが既知の連続バッファ
view = memoryview(pcm)
fill_pcm(view)                       # スライスへ直接書き込み（コピーなし）
```
