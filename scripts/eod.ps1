[CmdletBinding()]
param(
    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')][string]$AsOf,
    [switch]$EnforceFreshness,
    [string]$DatabasePath
)

. (Join-Path $PSScriptRoot "_common.ps1")
$root = Get-ProjectRoot
$uv = Get-UvCommand
if ($DatabasePath) {
    $env:QUANT_DB_PATH = Resolve-ProjectPath -Path $DatabasePath -ProjectRoot $root
}

$arguments = @("run", "quant-eod")
if ($AsOf) { $arguments += @("--as-of", $AsOf) }
if ($EnforceFreshness) { $arguments += "--enforce-freshness" }

Push-Location $root
try {
    & $uv @arguments
    if ($LASTEXITCODE -ne 0) { throw "日终流水线失败（exit code $LASTEXITCODE）。" }
} finally {
    Pop-Location
}
