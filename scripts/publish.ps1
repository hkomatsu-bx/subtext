# Subtext 配布ビルド（Windows / PowerShell）。
#
# Unit A（録音）と Unit C（ライブ字幕）を self-contained で publish し dist/ に出力する。
# self-contained は .NET ランタイムを同梱するため、配布先PCに .NET SDK/ランタイムが
# 不要になる（運用専用PCの移植が容易になる）。会議ハーネスは dist/recorder を優先的に
# 参照する（TargetFramework を直書きしない = runner.resolve_recorder_exe）。
#
# Unit B（会議後パイプライン, Python）は publish しない。TUI（tools/meeting）が uv 起動で
# ある以上、配布先にも uv とリポジトリ一式が要り、`uv sync` で Unit B も .venv へ入る。
# CPython を同梱しても使われないため、Unit B は `uv run subtext-postmeeting` に一本化する。
#
# 使い方（リポジトリルートで実行）:
#   pwsh -NoProfile -File scripts/publish.ps1
#
# 前提: .NET 10 SDK が PATH 上にあること（ビルドPC側）。配布先で不要になるのは .NET SDK だけで、
# uv は配布先にも要る（TUI = tools/meeting は publish 対象外で `uv run meeting tui` 起動のため）。
# 注意: WASAPI 依存のため Windows 専用。RID は win-x64 固定。
$ErrorActionPreference = 'Stop'

# 出力を UTF-8 に固定する。既定では Write-Host の日本語が端末のコードページ（CP932）で
# 書かれ、ログへリダイレクトすると化ける（dotnet 側の出力は UTF-8 なので混在する）。
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# リポジトリルート = このスクリプトの1つ上。
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Rid = 'win-x64'
$Config = 'Release'

function Publish-Unit([string]$Project, [string]$OutDir) {
    Write-Host "==> publish $Project -> $OutDir ($Rid, self-contained)"
    # dotnet publish は出力先を掃除しない。削除・改名された旧 DLL が残ると self-contained では
    # それもアセンブリ探索の対象になるため、毎回作り直す。
    if (Test-Path $OutDir) { Remove-Item -Recurse -Force $OutDir }
    dotnet publish $Project -c $Config -r $Rid --self-contained `
        -o $OutDir --nologo
    if ($LASTEXITCODE -ne 0) { throw "publish failed: $Project" }
}

Publish-Unit 'src/recorder' 'dist/recorder'
Publish-Unit 'src/live'     'dist/live'

Write-Host ''
Write-Host '完了: dist/recorder, dist/live を生成しました。'
Write-Host '会議ハーネスは dist/recorder（録音）と dist/live（ライブ字幕）を自動で優先参照します。'
Write-Host 'Unit B（議事録）は uv run で起動します（配布先でも uv sync が要ります）。'
