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
    $baseFull = [IO.Path]::GetFullPath($BasePath)
    $childFull = [IO.Path]::GetFullPath($ChildPath)
    $separator = [IO.Path]::DirectorySeparatorChar
    $trimChars = [char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    $baseRoot = [IO.Path]::GetPathRoot($baseFull)
    $childRoot = [IO.Path]::GetPathRoot($childFull)
    $isWindows = [Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT
    $comparison = if ($isWindows) { [StringComparison]::OrdinalIgnoreCase } else { [StringComparison]::Ordinal }
    if (-not [string]::Equals($baseRoot, $childRoot, $comparison)) {
        throw "Paths are on different roots: $baseRoot and $childRoot"
    }

    $baseRemainder = $baseFull.Substring($baseRoot.Length).Trim($trimChars)
    $childRemainder = $childFull.Substring($childRoot.Length).Trim($trimChars)
    $baseParts = if ($baseRemainder) { @($baseRemainder.Split($trimChars, [StringSplitOptions]::RemoveEmptyEntries)) } else { @() }
    $childParts = if ($childRemainder) { @($childRemainder.Split($trimChars, [StringSplitOptions]::RemoveEmptyEntries)) } else { @() }
    $common = 0
    while ($common -lt $baseParts.Count -and $common -lt $childParts.Count -and
           [string]::Equals($baseParts[$common], $childParts[$common], $comparison)) {
        $common++
    }
    $segments = @()
    for ($index = $common; $index -lt $baseParts.Count; $index++) { $segments += '..' }
    for ($index = $common; $index -lt $childParts.Count; $index++) { $segments += $childParts[$index] }
    if ($segments.Count -eq 0) { return '.' }
    return ($segments -join $separator)
}
