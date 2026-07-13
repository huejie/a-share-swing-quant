[CmdletBinding()]
param(
    [string]$DatabasePath,
    [string]$DestinationRoot,
    [string[]]$ArtifactPaths = @("data\research", "data\audit")
)

. (Join-Path $PSScriptRoot "_common.ps1")
$root = Get-ProjectRoot
$python = Get-PythonCommand -ProjectRoot $root
if (-not $DatabasePath) { $DatabasePath = if ($env:QUANT_DB_PATH) { $env:QUANT_DB_PATH } else { "data\quant_system.db" } }
if (-not $DestinationRoot) { $DestinationRoot = "backups" }
$database = Resolve-ProjectPath -Path $DatabasePath -ProjectRoot $root
$destination = Resolve-ProjectPath -Path $DestinationRoot -ProjectRoot $root
if (-not (Test-Path -LiteralPath $database -PathType Leaf)) { throw "SQLite 数据库不存在：$database" }

$stamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$backup = Join-Path $destination "backup-$stamp"
if (Test-Path -LiteralPath $backup) { throw "备份目录已存在：$backup" }
New-Item -ItemType Directory -Path (Join-Path $backup "database") -Force | Out-Null
$backupDb = Join-Path $backup "database\quant_system.db"

& $python (Join-Path $PSScriptRoot "sqlite_tools.py") backup $database $backupDb
if ($LASTEXITCODE -ne 0) { throw "SQLite 在线备份或 integrity_check 失败。" }

foreach ($artifactPath in $ArtifactPaths) {
    $source = Resolve-ProjectPath -Path $artifactPath -ProjectRoot $root
    if (-not (Test-Path -LiteralPath $source -PathType Container)) { continue }
    $name = Split-Path $source -Leaf
    $targetRoot = Join-Path $backup ("artifacts\" + $name)
    foreach ($file in Get-ChildItem -LiteralPath $source -Recurse -File) {
        $relative = Get-RelativePathCompat -BasePath $source -ChildPath $file.FullName
        $target = Join-Path $targetRoot $relative
        New-Item -ItemType Directory -Force -Path (Split-Path $target -Parent) | Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $target
    }
}

$entries = foreach ($file in Get-ChildItem -LiteralPath $backup -Recurse -File | Where-Object Name -ne "manifest.json") {
    [ordered]@{
        path = ((Get-RelativePathCompat -BasePath $backup -ChildPath $file.FullName) -replace '\\','/')
        length = $file.Length
        sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}
$manifest = [ordered]@{
    schema_version = 1
    created_at = (Get-Date).ToString("o")
    source_database = $database
    sqlite_integrity = "ok"
    automatic_trading = $false
    files = @($entries | Sort-Object path)
}
$manifestPath = Join-Path $backup "manifest.json"
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifestPath -Encoding utf8
Write-Output $backup
