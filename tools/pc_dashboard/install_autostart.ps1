# PCS dashboard autostart (2026-11-08).
# Creates a Task Scheduler task "PCS Dashboard" for the current user: starts 30 s after you log on (HWiNFO first),
# runs pcs_dash.py with pyw (no console window) and --no-browser, restarts it every minute if it dies (up to 999x),
# never stops it for running long, and runs on battery too. Open the page yourself: http://127.0.0.1:8570
#
# Why not a Windows service: a service runs as SYSTEM in session 0. The dashboard writes the FanControl requests to
# YOUR HKCU (HWiNFO custom sensors) and reads HWiNFO, which runs as you, so it has to live in your session.
#
#   install:    powershell -ExecutionPolicy Bypass -File install_autostart.ps1
#   remove:     powershell -ExecutionPolicy Bypass -File install_autostart.ps1 -Remove
#   stop now:   Stop-ScheduledTask -TaskName "PCS Dashboard"      (hard stop: the Mega parks 10 s later via lnk)
#   start now:  Start-ScheduledTask -TaskName "PCS Dashboard"
param([switch]$Remove)

$name = "PCS Dashboard"
if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed '$name'."
    return
}

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyw = (Get-Command pyw.exe -ErrorAction SilentlyContinue).Source
if (-not $pyw) { throw "pyw.exe (Python launcher, windowed) not found. Is Python installed for this user?" }

$action = New-ScheduledTaskAction -Execute $pyw -Argument "`"$here\pcs_dash.py`" --no-browser" -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$trigger.Delay = "PT30S"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "PCS outdoor cooling dashboard (tools\pc_dashboard\pcs_dash.py). Page: http://127.0.0.1:8570" -Force | Out-Null
Write-Host "Installed '$name': starts 30 s after logon with $pyw, restarts on failure. Page: http://127.0.0.1:8570"
