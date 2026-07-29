# Subtext 初期セットアップ（Windows / PowerShell）。別PCでの初回導入を数手に縮める。
#
# 実行内容:
#   1. 前提ツール（uv / dotnet）の存在確認
#   2. 単一 workspace venv を作成（uv sync。Python 3.13 は追跡済みの .python-version が固定する）
#   3. .env が無ければ .env.example から複製（値は手動記入）
#   4. I/O ディレクトリ（data/recordings, data/out）を作成
#
# 使い方（リポジトリルートで実行）:
#   pwsh -NoProfile -File scripts/bootstrap.ps1
#
# 補足: 録音/ライブ字幕の実行には .NET が要る。運用専用PCは scripts/publish.ps1 で
# 生成した dist/ を配布すれば SDK 不要。dotnet が無くても本スクリプトは警告のみで続行する。
$ErrorActionPreference = 'Stop'

# 出力を UTF-8 に固定する。既定では Write-Host の日本語が端末のコードページ（CP932）で
# 書かれ、ログへリダイレクトすると化ける。
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Test-Command([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

# 1) 前提ツール確認。
if (-not (Test-Command 'uv')) {
    throw "uv が見つかりません。https://docs.astral.sh/uv/ を参照して導入してください。"
}
if (-not (Test-Command 'dotnet')) {
    Write-Warning "dotnet が見つかりません。録音/ライブ字幕を動かすには .NET 10 SDK か、配布された dist/ が必要です。"
}

# 2) 単一 venv を同期。Python 3.13 の固定は追跡済みの .python-version が担うため pin はしない
#    （pin を挟むと失敗が黙殺される経路が増えるだけで、得るものが無い）。
Write-Host '==> uv sync（workspace: postmeeting + meeting を単一 .venv へ。Python 3.13 は .python-version で固定）'
uv sync
if ($LASTEXITCODE -ne 0) { throw 'uv sync に失敗しました。' }

# 3) .env 準備（無ければテンプレートから複製）。
if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
    Write-Host '==> .env を .env.example から作成しました。S3_BUCKET / BEDROCK_MODEL_ID を記入してください。'
} else {
    Write-Host '==> .env は既に存在します（上書きしません）。'
}

# 4) I/O ディレクトリ作成。
New-Item -ItemType Directory -Force -Path 'data/recordings', 'data/out' | Out-Null
Write-Host '==> data/recordings, data/out を用意しました。'

Write-Host ''
Write-Host 'セットアップ完了。ルートから次を実行できます:'
Write-Host '  uv run meeting status              # 現在の段を確認'
Write-Host '  uv run meeting record <分>         # 録音（別ターミナル・実機WASAPI）'
Write-Host '  uv run meeting minutes [session]   # 議事録（AWS 課金）'
