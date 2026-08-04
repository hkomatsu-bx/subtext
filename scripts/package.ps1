# Subtext 配布パッケージ作成（Windows / PowerShell）。
#
# 運用専用PCへ渡す「配布フォルダ」1 式を zip にまとめ、SHA256 と GitHub Release 用の
# 説明文の雛形を出力する。中身は次の 2 つの合成物で、マニュアル §1-2 の説明と 1:1 で対応する。
#
#   1. リポジトリ追跡分（`git archive` = 追跡ファイルのみ）。`Subtext.sln` を目印にルートを
#      解決するため一式が必要で、一部だけ抜き出したものでは動かない。
#   2. dist/（`publish.ps1` の self-contained 出力）。リポジトリに含まれないため、
#      `git clone` では付いてこない。
#
# `git archive` を使うのは、追跡外のものを混ぜないためである。`.env`（秘密）と `data/`
# （音声・実名を含む成果物）は追跡外なので、この方法なら構造上パッケージへ入らない（BR-SEC-01）。
#
# 使い方（リポジトリルートで実行）:
#   pwsh -NoProfile -File scripts/publish.ps1     # 先に dist/ を作る
#   pwsh -NoProfile -File scripts/package.ps1
#
# 前提: git が PATH 上にあること。作業ツリーの HEAD の内容がパッケージされる（未コミットの
# 変更は入らない）。出力は build/ 配下（gitignore 済み）。
$ErrorActionPreference = 'Stop'

# 出力を UTF-8 に固定する（publish.ps1 と同じ理由: CP932 でログが化けるのを防ぐ）。
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# リポジトリルート = このスクリプトの1つ上。
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Rid = 'win-x64'
$OutDir = Join-Path $Root 'build'
$Staging = Join-Path $OutDir 'package'

# --- バージョンの解決と一致確認 -----------------------------------------------
# 定義箇所は C# 1 つ＋Python 4 つ（CLAUDE.md「製品バージョン」）。1 つでも食い違えば、
# TUI が出す版と exe の版が違う配布物ができてしまうため、ここで止める。
function Get-DeclaredVersion {
    param([string]$Path, [string]$Pattern)
    $line = Select-String -Path (Join-Path $Root $Path) -Pattern $Pattern | Select-Object -First 1
    if (-not $line) { throw "バージョンを読み取れません: $Path" }
    return $line.Matches[0].Groups[1].Value
}

$declared = [ordered]@{
    'Directory.Build.props'           = Get-DeclaredVersion 'Directory.Build.props' '<Version>([^<]+)</Version>'
    'pyproject.toml'                  = Get-DeclaredVersion 'pyproject.toml' '^version = "([^"]+)"'
    'src/postmeeting/pyproject.toml'  = Get-DeclaredVersion 'src/postmeeting/pyproject.toml' '^version = "([^"]+)"'
    'tools/meeting/pyproject.toml'    = Get-DeclaredVersion 'tools/meeting/pyproject.toml' '^version = "([^"]+)"'
    'infra/pyproject.toml'            = Get-DeclaredVersion 'infra/pyproject.toml' '^version = "([^"]+)"'
}
# @() で必ず配列にする。1 件のときスカラー文字列になり、$unique[0] が「先頭の文字」を返す。
$unique = @($declared.Values | Sort-Object -Unique)
if ($unique.Count -ne 1) {
    $declared.GetEnumerator() | ForEach-Object { Write-Host ("  {0,-32} {1}" -f $_.Key, $_.Value) }
    throw "バージョンが揃っていません。全て同じ番号にしてから再実行してください（CLAUDE.md「製品バージョン」）。"
}
$Version = $unique[0]
Write-Host "バージョン: $Version（5 か所すべて一致）"

# --- 前提の確認 ---------------------------------------------------------------
foreach ($unit in @('recorder', 'live')) {
    $exe = Join-Path $Root "dist/$unit"
    if (-not (Test-Path $exe)) {
        throw "dist/$unit がありません。先に `pwsh -NoProfile -File scripts/publish.ps1` を実行してください。"
    }
}

$commit = (git rev-parse --short HEAD).Trim()
$dirty = (git status --porcelain) -ne $null
if ($dirty) {
    Write-Host "警告: 未コミットの変更があります。パッケージには HEAD（$commit）の内容が入ります。" -ForegroundColor Yellow
}

# --- ステージング --------------------------------------------------------------
if (Test-Path $Staging) { Remove-Item $Staging -Recurse -Force }
New-Item -ItemType Directory -Path $Staging -Force | Out-Null

# 追跡ファイルのみを取り出す。zip 経由にするのは、PowerShell のパイプがテキスト扱いで
# tar のバイナリストリームを壊すため（`git archive | tar -x` は使えない）。
$repoZip = Join-Path $OutDir 'repo.zip'
git archive --format=zip --output=$repoZip HEAD
if ($LASTEXITCODE -ne 0) { throw 'git archive に失敗しました。' }
Expand-Archive -Path $repoZip -DestinationPath $Staging -Force
Remove-Item $repoZip -Force

Copy-Item -Path (Join-Path $Root 'dist') -Destination $Staging -Recurse -Force

# --- zip 化 -------------------------------------------------------------------
# Compress-Archive ではなく .NET を直接使う（240MB 規模ではこちらが速く、メモリも食わない）。
Add-Type -AssemblyName System.IO.Compression.FileSystem
$Package = Join-Path $OutDir "subtext-$Version-$Rid.zip"
if (Test-Path $Package) { Remove-Item $Package -Force }
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $Staging, $Package, [System.IO.Compression.CompressionLevel]::Optimal, $false)
Remove-Item $Staging -Recurse -Force

$hash = (Get-FileHash -Path $Package -Algorithm SHA256).Hash
$sizeMb = [math]::Round((Get-Item $Package).Length / 1MB, 1)

Write-Host ""
Write-Host "パッケージ: $Package（$sizeMb MB）"
Write-Host "SHA256    : $hash"

# --- Release 説明文の雛形 ------------------------------------------------------
# そのまま `gh release create --notes-file` へ渡せる形で出す（版・SHA256 を手で書き写さない）。
$notes = @"
Subtext $Version（$Rid）

運用専用PC向けの配布物です。展開して ``scripts/bootstrap.ps1`` を 1 回流してください。
手順は同梱の ``docs/会議議事録作成マニュアル.html`` §1 を参照してください。

**同梱物**

- リポジトリ一式（``Subtext.sln`` を含む。会議ハーネスがルートの目印に使います）
- ``dist/recorder``・``dist/live``（self-contained。配布先に .NET SDK は不要です）

``.env`` と ``data/`` は含みません（設定は ``bootstrap.ps1`` が雛形を作ります）。

**版**

C# と Python は同じ $Version です。実行ファイルの詳細プロパティには
ビルド元コミット（``$Version+<sha>``）が入ります。ビルド元: ``$commit``

**受け取ったら**

zip でダウンロードしたファイルには Windows が「別の PC から来た」印を付け、
スクリプトの実行を止めます。展開後に 1 回だけ次を実行してください。

``````powershell
Unblock-File -Path scripts\*.ps1
``````

**SHA256**

``````
$hash  subtext-$Version-$Rid.zip
``````
"@
$notesPath = Join-Path $OutDir "release-notes-$Version.md"
Set-Content -Path $notesPath -Value $notes -Encoding utf8
Write-Host "説明文の雛形: $notesPath"
Write-Host ""
Write-Host "次の手順（外部公開のため、内容を確認してから実行してください）:" -ForegroundColor Cyan
Write-Host "  gh release create v$Version `"$Package`" --title `"Subtext $Version`" --notes-file `"$notesPath`""
