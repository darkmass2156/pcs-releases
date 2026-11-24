# PCS: keep the cooling chain responsive under full CPU load (owner 2026-11-13: the pump didn't react while a
# benchmark loaded every core). FanControl and HWiNFO run elevated, so only an admin can raise them.
#
# Run ONCE from an admin PowerShell:
#   powershell -ExecutionPolicy Bypass -File "<repo>\tools\pc_dashboard\priority_boost.ps1"
# It sets FanControl, HWiNFO64 and the PCS dashboard to High now, and registers the task "PCS Priority" (highest
# privileges) that does the same at logon (+2 min, after the startup chain) and every 5 minutes, so restarts of any
# of them are caught. High, not Realtime: Realtime can starve mouse / keyboard input. Undo:
#   Unregister-ScheduledTask -TaskName "PCS Priority" -Confirm:$false
param([switch]$Apply)   # -Apply: only set the priorities (what the task runs)

$names = @("FanControl", "HWiNFO64")

function Set-High {
    foreach ($n in $names) {
        Get-Process -Name $n -ErrorAction SilentlyContinue | ForEach-Object {
            try { $_.PriorityClass = "High" } catch {}
        }
    }
    # the dashboard: pythonw running pcs_dash.py
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' or Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "pcs_dash\.py" } | ForEach-Object {
            try { (Get-Process -Id $_.ProcessId).PriorityClass = "High" } catch {}
        }
}

function Ensure-Dashboard {
    # 2026-11-14: the dashboard died at logon (0xC0000005) and Task Scheduler's "restart on failure" only covers a
    # failed launch, not a crash later. This check (every 5 min) starts it again; the log says when.
    $up = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' or Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "pcs_dash\.py" }
    if (-not $up) {
        try {
            Start-ScheduledTask -TaskName "PCS Dashboard" -ErrorAction Stop
            $log = Join-Path $PSScriptRoot "logs\watchdog.log"
            Add-Content -Path $log -Value ("{0} dashboard was not running: started" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
        } catch {}
    }
}

function Ensure-Reader {
    # 2026-11-22: the standalone sensor reader drives the case fans; the dashboard restarts it after 20 s of stale data,
    # this is the backup when the dashboard is down too (a dead reader leaves the fans at their last duty)
    $t = Get-ScheduledTask -TaskName "PCS Sensors" -ErrorAction SilentlyContinue
    if ($t -and -not (Get-Process -Name "PcsSensors" -ErrorAction SilentlyContinue)) {
        try {
            Start-ScheduledTask -TaskName "PCS Sensors" -ErrorAction Stop
            $log = Join-Path $PSScriptRoot "logs\watchdog.log"
            Add-Content -Path $log -Value ("{0} sensor reader was not running: started" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
        } catch {}
    }
}

if ($Apply) { Ensure-Reader; Ensure-Dashboard; Start-Sleep -Seconds 5; Set-High; exit 0 }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Write-Host "Run this from an ADMIN PowerShell." -ForegroundColor Red; exit 1 }

Set-High
$me = $MyInvocation.MyCommand.Path
$act = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$me`" -Apply"
$logon = New-ScheduledTaskTrigger -AtLogOn
$logon.Delay = "PT2M"
$rep = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$who = [Security.Principal.WindowsIdentity]::GetCurrent().Name     # COMPUTER\user (a bare user name was rejected)
# S4U = "run whether the user is logged on or not" without a stored password: runs in the background, so no console
# window flashes every 5 min (2026-11-14; Interactive showed powershell.exe before -WindowStyle Hidden took effect)
$pr = New-ScheduledTaskPrincipal -UserId $who -LogonType S4U -RunLevel Highest
$ok = $true
try {
    Register-ScheduledTask -TaskName "PCS Priority" -Action $act -Trigger @($logon, $rep) -Settings $set -Principal $pr -Force -ErrorAction Stop | Out-Null
} catch { $ok = $false; Write-Host "Task NOT registered: $($_.Exception.Message)" -ForegroundColor Red }

foreach ($n in $names + @("pythonw")) {
    Get-Process -Name $n -ErrorAction SilentlyContinue | ForEach-Object { "{0,-12} {1,6}  {2}" -f $_.ProcessName, $_.Id, $_.PriorityClass }
}
if ($ok) { Write-Host "Task 'PCS Priority' registered for ${who}: High at logon (+2 min) and every 5 min." -ForegroundColor Green }
