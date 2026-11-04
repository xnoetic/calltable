# Install/update the calltable client (Windows).
#
#   powershell -ExecutionPolicy Bypass -File .\bin\install.ps1
#
# Clones or pulls into $HOME\.calltable\src, writes shims named `ct` (no
# extension, for Git Bash) and `ct.cmd` (for cmd/PowerShell) into $HOME\bin,
# and adds that directory to the user-level PATH (no admin rights needed).

[CmdletBinding()]
param(
    [string]$Src = (Join-Path $HOME ".calltable\src"),
    [string]$Bin = (Join-Path $HOME "bin"),
    [string]$RepoUrl = "https://github.com/eanzhao-os/calltable.git",
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

if (Test-Path (Join-Path $Src ".git")) {
    Write-Host "==> updating $Src"
    git -C $Src pull --ff-only
} else {
    Write-Host "==> cloning $RepoUrl -> $Src"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Src) | Out-Null
    git clone $RepoUrl $Src
}

New-Item -ItemType Directory -Force -Path $Bin | Out-Null

# A shim pinned to the install directory; independent of the current directory.
$shim = @"
@echo off
setlocal
set "CALLTABLE_ROOT=$Src"
set "PYTHONPATH=%CALLTABLE_ROOT%;%PYTHONPATH%"
$Python -m calltable cli %*
"@
Set-Content -Path (Join-Path $Bin "ct.cmd") -Value $shim -Encoding ASCII
Write-Host "==> wrote $(Join-Path $Bin 'ct.cmd')"

# Git Bash does not honour PATHEXT, so it needs an extension-less shim. The
# path stays in Windows form and is consumed by the Git Bash python.
$shShim = @'
#!/usr/bin/env bash
export CALLTABLE_ROOT="__CALLTABLE_ROOT__"
export PYTHONPATH="$CALLTABLE_ROOT${PYTHONPATH:+;$PYTHONPATH}"
exec __PYTHON__ -m calltable cli "$@"
'@
$shShim = $shShim.Replace("__CALLTABLE_ROOT__", $Src).Replace("__PYTHON__", $Python)
$shShim = $shShim.Replace("`r`n", "`n")
$shPath = Join-Path $Bin "ct"
[System.IO.File]::WriteAllText($shPath, $shShim,
    (New-Object System.Text.UTF8Encoding($false)))
Write-Host "==> wrote $shPath"

# Git for Windows' /etc/profile usually adds ~/bin to PATH already; add it once.
$bashrc = Join-Path $HOME ".bashrc"
$marker = "calltable-bin-path"
$hasMarker = (Test-Path $bashrc) -and
    (Select-String -Path $bashrc -SimpleMatch $marker -Quiet)
if (-not $hasMarker) {
    $block = @"

# ${marker}: make sure Git Bash can find ~/bin/ct (idempotent)
case ":`$PATH:" in
  *":`$HOME/bin:"*) ;;
  *) export PATH="`$HOME/bin:`$PATH" ;;
esac
"@
    $existing = if (Test-Path $bashrc) { [System.IO.File]::ReadAllText($bashrc) } else { "" }
    $text = $existing + $block.Replace("`r`n", "`n")
    [System.IO.File]::WriteAllText($bashrc, $text,
        (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "==> ensured ~/bin is on the Git Bash PATH via .bashrc"
}

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($null -eq $userPath) { $userPath = "" }
$parts = $userPath -split ';' | Where-Object { $_ -ne "" }
if ($parts -notcontains $Bin) {
    $newPath = (($parts + $Bin) -join ';')
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Host "==> added $Bin to the user PATH (open a new terminal to pick it up)"
} else {
    Write-Host "==> $Bin already on the user PATH"
}

try {
    & (Join-Path $Bin "ct.cmd") --version
} catch {
    Write-Host "!! could not run ct; check that Python 3.12+ is installed" -ForegroundColor Yellow
}

Write-Host "next: ct setup --hub http://hub.example.internal:8091 ; ct doctor"
