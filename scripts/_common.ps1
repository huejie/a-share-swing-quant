Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-ProjectRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

function Get-UvCommand {
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $fallback = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    if (Test-Path -LiteralPath $fallback -PathType Leaf) { return $fallback }
    throw "uv 未安装。请先安装 uv：https://docs.astral.sh/uv/"
}

function Get-PythonCommand {
    param([string]$ProjectRoot)
    $venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) { return $venvPython }
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    throw "找不到 Python。请先运行 scripts/test.ps1 -Install，或安装 Python 3.11+。"
}

function Resolve-ProjectPath {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)][string]$ProjectRoot
    )
    if ([IO.Path]::IsPathRooted($Path)) { return [IO.Path]::GetFullPath($Path) }
    return [IO.Path]::GetFullPath((Join-Path $ProjectRoot $Path))
}

function Get-RelativePathCompat {
    param(
        [Parameter(Mandatory=$true)][string]$BasePath,
        [Parameter(Mandatory=$true)][string]$ChildPath
    )
    $baseFull = [IO.Path]::GetFullPath($BasePath).TrimEnd('\') + '\'
    $childFull = [IO.Path]::GetFullPath($ChildPath)
    $baseUri = [Uri]$baseFull
    $childUri = [Uri]$childFull
    return [Uri]::UnescapeDataString($baseUri.MakeRelativeUri($childUri).ToString()).Replace('/', '\')
}
