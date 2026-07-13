[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$BackupPath,
    [Parameter(Mandatory=$true)][string]$RestoreDirectory
)

. (Join-Path $PSScriptRoot "_common.ps1")
$root = Get-ProjectRoot
$python = Get-PythonCommand -ProjectRoot $root
$backup = (Resolve-Path -LiteralPath (Resolve-ProjectPath -Path $BackupPath -ProjectRoot $root)).Path
$restore = Resolve-ProjectPath -Path $RestoreDirectory -ProjectRoot $root
if (-not (Test-Path -LiteralPath $backup -PathType Container)) { throw "备份目录不存在：$backup" }
if (Test-Path -LiteralPath $restore) { throw "恢复目标必须是显式指定且尚不存在的新目录：$restore" }
$manifestPath = Join-Path $backup "manifest.json"
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw "缺少 manifest.json：$backup" }

$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.schema_version -ne 1) { throw "不支持的 manifest schema：$($manifest.schema_version)" }
if ($manifest.automatic_trading -ne $false) { throw "备份边界声明异常。" }

function Test-ManifestFiles([string]$Base, $Files) {
    foreach ($entry in $Files) {
        $candidate = [IO.Path]::GetFullPath((Join-Path $Base ($entry.path -replace '/', '\')))
        $prefix = [IO.Path]::GetFullPath($Base).TrimEnd('\') + '\'
        if (-not $candidate.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw "manifest 包含越界路径：$($entry.path)" }
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { throw "备份文件缺失：$($entry.path)" }
        $file = Get-Item -LiteralPath $candidate
        if ($file.Length -ne [long]$entry.length) { throw "文件长度不匹配：$($entry.path)" }
        $hash = (Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($hash -ne $entry.sha256) { throw "SHA-256 不匹配：$($entry.path)" }
    }
}

# 复制前先验证源备份，避免把已损坏内容恢复到新位置。
Test-ManifestFiles -Base $backup -Files $manifest.files
New-Item -ItemType Directory -Path $restore | Out-Null
foreach ($entry in $manifest.files) {
    $source = Join-Path $backup ($entry.path -replace '/', '\')
    $target = Join-Path $restore ($entry.path -replace '/', '\')
    New-Item -ItemType Directory -Force -Path (Split-Path $target -Parent) | Out-Null
    Copy-Item -LiteralPath $source -Destination $target
}
Copy-Item -LiteralPath $manifestPath -Destination (Join-Path $restore "manifest.json")
Test-ManifestFiles -Base $restore -Files $manifest.files

$restoredDb = Join-Path $restore "database\quant_system.db"
& $python (Join-Path $PSScriptRoot "sqlite_tools.py") integrity $restoredDb
if ($LASTEXITCODE -ne 0) { throw "恢复副本 SQLite integrity_check 失败；未触碰源数据库。" }
Write-Output $restore
