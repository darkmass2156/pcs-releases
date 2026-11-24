# PCS Monitor - diagnostics (2026-11-23). Writes PCS-diagnostics.txt on the desktop: Windows version, CPU / GPU,
# WebView2, the PCS processes, the sensor task, the last Windows error / hang reports for PCS, and the PCS logs.
# Reads only; nothing is changed and nothing is sent anywhere. Send the file to whoever helps you.
$ErrorActionPreference = "SilentlyContinue"
$out = Join-Path ([Environment]::GetFolderPath("Desktop")) "PCS-diagnostics.txt"
$sb = New-Object System.Text.StringBuilder
function H($t) { [void]$sb.AppendLine(""); [void]$sb.AppendLine("==== $t ====") }
function L($t) { [void]$sb.AppendLine(($t | Out-String -Width 220).TrimEnd()) }
function Tail($path, $n = 300) {
    if (Test-Path $path) { L "-- $path ($((Get-Item $path).Length) bytes)"; L (Get-Content $path -Tail $n) } else { L "-- ${path}: none" }
}

H "system"
L ("collected {0}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
$os = Get-CimInstance Win32_OperatingSystem
L ("{0} {1} build {2}, uptime {3:N1} h" -f $os.Caption, $os.OSArchitecture, $os.BuildNumber, ((Get-Date) - $os.LastBootUpTime).TotalHours)
L (Get-CimInstance Win32_Processor | Select-Object Name, NumberOfCores, NumberOfLogicalProcessors | Format-List)
L (Get-CimInstance Win32_VideoController | Select-Object Name, DriverVersion | Format-List)
L (Get-CimInstance Win32_BaseBoard | Select-Object Manufacturer, Product | Format-List)
L ("RAM {0:N1} GB" -f ((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB))
$wv = Get-ItemProperty "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
if (-not $wv) { $wv = Get-ItemProperty "HKCU:\Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" }
L ("WebView2 runtime: {0}" -f ($(if ($wv) { $wv.pv } else { "NOT FOUND" })))
$pawn = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO"
L ("PawnIO: {0}" -f ($(if ($pawn) { $pawn.DisplayVersion } else { "not installed" })))
L ("Other sensor tools running: {0}" -f ((Get-Process HWiNFO64, FanControl, LibreHardwareMonitor, OpenHardwareMonitor, HWMonitor, AIDA64, iCUE, ArmouryCrate*, LightingService | Select-Object -Expand Name -Unique) -join ", "))

H "PCS processes"
L (Get-Process PCS, PcsService, PcsSensors, msedgewebview2 | Select-Object Name, Id, Responding, @{n="CPU s";e={[math]::Round($_.CPU,1)}},
    @{n="MB";e={[math]::Round($_.WorkingSet64/1MB)}}, StartTime, Path | Format-Table -AutoSize)
try { L ("service /status: " + (Invoke-WebRequest http://127.0.0.1:8570/status -UseBasicParsing -TimeoutSec 5).StatusCode) }
catch { L ("service /status: NO ANSWER (" + $_.Exception.Message + ")") }

H "sensor task"
L (Get-ScheduledTask -TaskName "PCS Sensors" | Select-Object TaskName, State | Format-List)
L (Get-ScheduledTaskInfo -TaskName "PCS Sensors" | Select-Object LastRunTime, LastTaskResult, NumberOfMissedRuns | Format-List)
$now = Join-Path $env:ProgramData "PCS\sensors\now.json"
if (Test-Path $now) {
    $age = ((Get-Date) - (Get-Item $now).LastWriteTime).TotalSeconds
    L ("now.json: {0:N0} s old, {1} bytes" -f $age, (Get-Item $now).Length)
    try {
        $j = Get-Content $now -Raw | ConvertFrom-Json
        L ("admin {0}, loop {1} ms, errors {2}, shm {3}, sensors {4}" -f $j.admin, $j.loop_ms, $j.errors, $j.shm, $j.sensors.Count)
        L ("cpu_info: " + ($j.cpu_info | ConvertTo-Json -Compress))
        L ("msr: " + $j.msr.error)
        L ("poll_ms: " + ($j.poll_ms | ConvertTo-Json -Compress))
        L ("slowest_pass: " + ($j.slowest_pass | ConvertTo-Json -Compress))
        L ("hardware: " + (($j.sensors | ForEach-Object { $_.hw } | Select-Object -Unique) -join " | "))
    } catch { L ("now.json unreadable: " + $_.Exception.Message) }
} else { L "now.json: missing (the sensor reader never wrote)" }

H "Windows error / hang reports for PCS (last 3 days)"
$since = (Get-Date).AddDays(-3)
$ev = Get-WinEvent -FilterHashtable @{ LogName = "Application"; Id = 1000, 1001, 1002, 1026; StartTime = $since } |
    Where-Object { $_.Message -match "PCS\.exe|PcsService|PcsSensors|msedgewebview2" } | Select-Object -First 15
if ($ev) { foreach ($e in $ev) { L ("--- {0} id {1} {2}" -f $e.TimeCreated, $e.Id, $e.ProviderName); L $e.Message } } else { L "none" }

H "PCS logs"
$la = Join-Path $env:LOCALAPPDATA "PCS"
Tail (Join-Path $la "app.log")
Tail (Join-Path $la "data\service.log")
Tail (Join-Path $la "data\watchdog.log") 50

[IO.File]::WriteAllText($out, $sb.ToString())
Write-Host "Written: $out" -ForegroundColor Green
Write-Host "Send that file back. It contains your PC's hardware names, nothing personal beyond that."
