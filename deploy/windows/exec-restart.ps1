# Cleanly restart a calltable executor scheduled task.
#
# Windows' Stop-ScheduledTask only stops the cmd.exe launcher; the python child
# keeps running and keeps claiming jobs. This script (1) drains the host on the
# hub so no new job is dispatched, (2) waits until the running jobs finish, then
# (3) stops the task, kills the actual `-m calltable exec` process, starts the
# task again and (4) undrains the host.
#
#   powershell -ExecutionPolicy Bypass -File .\exec-restart.ps1
#   powershell -ExecutionPolicy Bypass -File .\exec-restart.ps1 -TaskName calltable-executor
#   powershell -ExecutionPolicy Bypass -File .\exec-restart.ps1 -SkipDrain
#
# -SkipDrain is only for hosts that are already idle (nothing to protect).
#
[CmdletBinding()]
param(
    [string]$TaskName = "calltable-executor",
    [string]$ExecConfig = "",
    [switch]$SkipDrain,
    [int]$DrainTimeoutMinutes = 720
)

$ErrorActionPreference = "Stop"

if (-not $ExecConfig) {
    $ExecConfig = Join-Path (Get-Location) "config.exec.json"
}

function Get-ExecConfig {
    if (-not (Test-Path $ExecConfig)) {
        throw "executor config not found: $ExecConfig"
    }
    return Get-Content $ExecConfig -Raw | ConvertFrom-Json
}

function Invoke-Hub([string]$Method, [string]$Path, $Body) {
    $cfg = Get-ExecConfig
    $headers = @{ Authorization = "Bearer $($cfg.token)" }
    $uri = "$($cfg.hub_url.TrimEnd('/'))$Path"
    if ($null -ne $Body) {
        $json = $Body | ConvertTo-Json -Compress
        return Invoke-RestMethod -Method $Method -Uri $uri -Headers $headers `
            -ContentType "application/json" -Body $json -TimeoutSec 30
    }
    return Invoke-RestMethod -Method $Method -Uri $uri -Headers $headers -TimeoutSec 30
}

function Set-Drain([string]$Name, [bool]$Drain) {
    $body = @{ drain = $Drain }
    return Invoke-Hub "POST" "/api/hosts/$Name/drain" $body
}

function Wait-ForHostIdle([string]$Name, [int]$TimeoutMinutes) {
    $deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    while ((Get-Date) -lt $deadline) {
        $state = Invoke-Hub "GET" "/api/state" $null
        # Note: $Host is a read-only PowerShell automatic variable; do not use it.
        $entry = @($state.hosts | Where-Object { $_.name -eq $Name })[0]
        if (-not $entry) { return $true }
        $running = @($entry.running).Count
        if ($running -eq 0) { return $true }
        Write-Host "  $Name still has $running running job(s); waiting 60s"
        Start-Sleep -Seconds 60
    }
    return $false
}

function Stop-WindowsExecutors {
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match '-m\s+calltable\s+exec' }
    foreach ($proc in $procs) {
        Write-Host "  stopping python $($proc.ProcessId)"
        try { Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop } catch {
            Write-Host "  (already gone)"
        }
    }
    return @($procs).Count
}

function Restart-One([string]$name) {
    if (-not (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue)) {
        Write-Host "task $name not found; skip"
        return
    }
    $cfg = Get-ExecConfig
    $hostName = "$($cfg.exec_host)"
    Write-Host "== $name (host $hostName)"

    if (-not $SkipDrain) {
        [void](Set-Drain $hostName $true)
        Write-Host "  drained $hostName on the hub"
        if (-not (Wait-ForHostIdle $hostName $DrainTimeoutMinutes)) {
            [void](Set-Drain $hostName $false)
            throw "$hostName still has running jobs after $DrainTimeoutMinutes minutes; " +
                  "not restarting"
        }
        Write-Host "  $hostName is idle"
    }

    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    [void](Stop-WindowsExecutors)
    Start-Sleep -Seconds 2
    Start-ScheduledTask -TaskName $name
    Start-Sleep -Seconds 3
    $info = Get-ScheduledTaskInfo -TaskName $name
    Write-Host "  restarted; last result=$($info.LastTaskResult)"
    if (-not $SkipDrain) {
        [void](Set-Drain $hostName $false)
        Write-Host "  undrained $hostName"
    }
}

Restart-One $TaskName
