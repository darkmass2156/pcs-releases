# Registers the standalone sensor reader as a scheduled task and starts it (run once, as administrator).
#   Right-click PowerShell > Run as administrator, then:
#     powershell -ExecutionPolicy Bypass -File "<repo>\tools\pc_sensors\install_sensors.ps1"
#   -Remove stops and deletes the task.
# The reader needs administrator rights for the PawnIO driver (CPU registers, Super I/O / EC). The task runs it with the
# highest privileges, logon type S4U (no console window), at logon + 15 s, restarted if it dies.
param([switch]$Remove)
$ErrorActionPreference = "Stop"
$name = "PCS Sensors"
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Write-Host "Run this from an administrator PowerShell." -ForegroundColor Red; exit 1 }

if ($Remove) {
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed task '$name'."
    exit 0
}

$exe = Join-Path $PSScriptRoot "bin\PcsSensors.exe"
if (-not (Test-Path $exe)) { Write-Host "Not built: $exe missing (dotnet build -c Release in $PSScriptRoot)." -ForegroundColor Red; exit 1 }
$who = "$env:USERDOMAIN\$env:USERNAME"
$act = New-ScheduledTaskAction -Execute $exe -WorkingDirectory (Split-Path $exe)
$trg = New-ScheduledTaskTrigger -AtLogOn -User $who
$trg.Delay = "PT15S"
$pri = New-ScheduledTaskPrincipal -UserId $who -LogonType S4U -RunLevel Highest
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask -TaskName $name -Action $act -Trigger $trg -Principal $pri -Settings $set -Force | Out-Null
Start-ScheduledTask -TaskName $name
Start-Sleep 6
$now = Join-Path $PSScriptRoot "..\pc_dashboard\logs\sensors\now.json"
if (Test-Path $now) {
    $j = Get-Content $now -Raw | ConvertFrom-Json
    Write-Host ("Running: admin={0}, {1} sensors." -f $j.admin, $j.sensors.Count) -ForegroundColor Green
} else {
    Write-Host "Task started, but no output yet: check Task Scheduler > '$name' > History." -ForegroundColor Yellow
}
