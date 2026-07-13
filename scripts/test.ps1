[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$SkipFrontend,
    [switch]$SkipBuild
)

. (Join-Path $PSScriptRoot "_common.ps1")
$root = Get-ProjectRoot
$uv = Get-UvCommand
$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
$npm = if ($npmCommand) { $npmCommand.Source } else { $null }

if ($Install) {
    & $uv sync --extra dev
    if ($LASTEXITCODE -ne 0) { throw "Python 依赖安装失败。" }
    if (-not $SkipFrontend) {
        if (-not $npm) { throw "找不到 npm.cmd；请安装 Node.js。" }
        & $npm --prefix (Join-Path $root "apps\web") install
        if ($LASTEXITCODE -ne 0) { throw "前端依赖安装失败。" }
    }
}

Push-Location $root
try {
    & $uv run --extra dev pytest tests -q
    if ($LASTEXITCODE -ne 0) { throw "Python/运维测试失败。" }

    if (-not $SkipFrontend) {
        if (-not $npm) { throw "找不到 npm.cmd；请安装 Node.js。" }
        & $npm --prefix (Join-Path $root "apps\web") test
        if ($LASTEXITCODE -ne 0) { throw "前端测试失败。" }
        if (-not $SkipBuild) {
            & $npm --prefix (Join-Path $root "apps\web") run build
            if ($LASTEXITCODE -ne 0) { throw "前端生产构建失败。" }
        }
    }
} finally {
    Pop-Location
}
Write-Host "全部选定检查通过。"
