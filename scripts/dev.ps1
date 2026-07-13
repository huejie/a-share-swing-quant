[CmdletBinding()]
param(
    [switch]$Install,
    [int]$ApiPort = 8000,
    [int]$WebPort = 5173,
    [string]$DatabasePath
)

. (Join-Path $PSScriptRoot "_common.ps1")
$root = Get-ProjectRoot
$uv = Get-UvCommand
$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
$npm = if ($npmCommand) { $npmCommand.Source } else { $null }
if (-not $npm) { throw "找不到 npm.cmd；请安装 Node.js。" }

if ($Install) {
    & $uv sync --extra dev
    if ($LASTEXITCODE -ne 0) { throw "Python 依赖安装失败。" }
    & $npm --prefix (Join-Path $root "apps\web") install
    if ($LASTEXITCODE -ne 0) { throw "前端依赖安装失败。" }
}

$python = Get-PythonCommand -ProjectRoot $root
$nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
if (-not $nodeCommand) { throw "找不到 node.exe；请安装 Node.js。" }
$vite = Join-Path $root "apps\web\node_modules\vite\bin\vite.js"
if (-not (Test-Path -LiteralPath $vite -PathType Leaf)) { throw "前端依赖尚未安装；请运行 scripts/dev.ps1 -Install。" }

$runtime = Join-Path $root "data\runtime"
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
if ($DatabasePath) {
    $env:QUANT_DB_PATH = Resolve-ProjectPath -Path $DatabasePath -ProjectRoot $root
}

$apiOut = Join-Path $runtime "api.stdout.log"
$apiErr = Join-Path $runtime "api.stderr.log"
$webOut = Join-Path $runtime "web.stdout.log"
$webErr = Join-Path $runtime "web.stderr.log"

# 直接启动最终 Python/Node 进程，使记录的 PID 就是监听进程；停止时不会遗留 reloader/cmd 子进程。
$api = Start-Process -FilePath $python -ArgumentList @("-m", "uvicorn", "apps.api.main:app", "--host", "127.0.0.1", "--port", $ApiPort) -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput $apiOut -RedirectStandardError $apiErr -PassThru
$web = Start-Process -FilePath $nodeCommand.Source -ArgumentList @($vite, "--host", "127.0.0.1", "--port", $WebPort) -WorkingDirectory (Join-Path $root "apps\web") -WindowStyle Hidden -RedirectStandardOutput $webOut -RedirectStandardError $webErr -PassThru

$state = [ordered]@{
    started_at = (Get-Date).ToString("o")
    api = @{ pid = $api.Id; url = "http://127.0.0.1:$ApiPort"; stdout = $apiOut; stderr = $apiErr }
    web = @{ pid = $web.Id; url = "http://127.0.0.1:$WebPort"; stdout = $webOut; stderr = $webErr }
    automatic_trading = $false
}
$statePath = Join-Path $runtime "dev-processes.json"
$state | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $statePath -Encoding utf8

Write-Host "API PID $($api.Id): http://127.0.0.1:$ApiPort"
Write-Host "Web PID $($web.Id): http://127.0.0.1:$WebPort"
Write-Host "进程与日志记录：$statePath"
Write-Host "停止时请执行：Stop-Process -Id $($api.Id),$($web.Id)"
