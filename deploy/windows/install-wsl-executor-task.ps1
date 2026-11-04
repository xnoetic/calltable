# Register a calltable executor that lives inside a WSL distribution.
#
# WSL usually has no systemd, so the executor runs in direct mode. A Windows
# logon task starts wsl.exe with the chosen distro/user and execs the WSL-side
# entry point (deploy/wsl/exec.sh). A launcher .cmd avoids nested cmd /s /c
# quoting problems.
#
#   powershell -ExecutionPolicy Bypass -File .\install-wsl-executor-task.ps1 `
#       -Distro Ubuntu-24.04 -User myuser -Name calltable-executor-wsl
#
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Distro,
    [Parameter(Mandatory = $true)][string]$User,
    [string]$Name = "calltable-executor-wsl",
    [string]$WslExe = "C:\Windows\System32\wsl.exe",
    [string]$Entry = "~/.calltable/src/deploy/wsl/exec.sh",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Host "removed $Name"
    }
    return
}

if (-not (Test-Path $WslExe)) { throw "wsl.exe not found: $WslExe" }

$logDir = Join-Path $env:LOCALAPPDATA "calltable"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$launcher = Join-Path $logDir "run-executor-wsl.cmd"
$log = Join-Path $logDir "executor-wsl.log"

$body = @"
@echo off
setlocal
"$WslExe" -d $Distro -u $User -- bash -lc "exec $Entry" >> "$log" 2>&1
"@
Set-Content -Path $launcher -Value $body -Encoding ASCII

$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument ('/d /c "' + $launcher + '"')
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false
}

Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description "calltable executor in WSL ($Distro, direct mode)" | Out-Null

Write-Host "registered $Name"
Write-Host "  launcher: $launcher"
Write-Host "  log     : $log"
Start-ScheduledTask -TaskName $Name
Write-Host "started; check with Get-ScheduledTaskInfo -TaskName $Name"
