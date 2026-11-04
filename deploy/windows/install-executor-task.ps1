<#
.SYNOPSIS
    Register the calltable executor as a "run at logon" scheduled task.

.DESCRIPTION
    Desktop UI tests (WebView2/Photino, real desktop checks) need an interactive
    desktop, so the executor must NOT run as a service: services run in Session
    0 with no desktop and no WebView2. This script registers a task with
    LogonType=Interactive that starts the executor in the user's own session and
    restarts it after a crash.

.PARAMETER Repo
    calltable checkout root (contains the calltable/ package).

.PARAMETER Config
    Executor config path; defaults to <Repo>\config.exec.json.

.PARAMETER Python
    python.exe path; defaults to `python` on PATH.

.PARAMETER Name
    Scheduled task name, default calltable-executor.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\deploy\windows\install-executor-task.ps1 -Repo C:\src\calltable

.NOTES
    Uninstall: .\install-executor-task.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$Repo = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),
    [string]$Config = "",
    [string]$Python = "",
    [string]$Name = "calltable-executor",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Host "removed scheduled task $Name"
    } else {
        Write-Host "scheduled task $Name does not exist"
    }
    return
}

$Repo = (Resolve-Path $Repo).Path
if (-not $Config) { $Config = Join-Path $Repo "config.exec.json" }
if (-not (Test-Path $Config)) { throw "executor config not found: $Config (copy config.example.json)" }
if (-not (Test-Path (Join-Path $Repo "calltable\__main__.py"))) {
    throw "$Repo does not look like a calltable checkout (missing calltable\__main__.py)"
}
if (-not $Python) {
    $Python = (Get-Command python).Source
}
if (-not (Test-Path $Python)) { throw "python not found: $Python" }

$logDir = Join-Path $env:LOCALAPPDATA "calltable"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "executor.log"

# The task itself cannot redirect output; wrap it in a launcher.cmd so nested
# cmd /s /c quoting does not split paths that contain spaces.
$launcher = Join-Path $logDir "run-executor.cmd"
$launcherBody = @"
@echo off
setlocal
cd /d "$Repo"
set "PYTHONPATH=$Repo;%PYTHONPATH%"
"$Python" -m calltable exec --config "$Config" >> "$log" 2>&1
"@
Set-Content -Path $launcher -Value $launcherBody -Encoding ASCII
$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument ('/d /c "' + $launcher + '"') -WorkingDirectory $Repo

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

# LogonType=Interactive: the task only runs in the current user's session.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false
}

Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description "calltable executor (interactive session)" | Out-Null

Write-Host "registered scheduled task $Name"
Write-Host "  repo   : $Repo"
Write-Host "  config : $Config"
Write-Host "  log    : $log"
Write-Host "  trigger: at logon of $env:USERNAME (interactive session)"
Start-ScheduledTask -TaskName $Name
Write-Host "started; check with Get-ScheduledTaskInfo -TaskName $Name"
