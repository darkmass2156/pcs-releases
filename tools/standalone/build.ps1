# Builds the PCS package for other PCs (2026-11-23): monitor + fan control, no controller.
#   powershell -ExecutionPolicy Bypass -File tools\standalone\build.ps1
# Needs: .NET 10 SDK, Python 3 with PyInstaller (py -m pip install pyinstaller), vendor\PawnIO_setup.exe (official
# release from github.com/namazso/PawnIO.Setup, signed by namazso.eu), release.json {"repo": "owner/name"} for updates.
# Output (out\):
#   PCS-Setup-<ver>.exe          one windowed setup (no console): wizard, or /update from the app (tools\pc_setup)
#   PCS-Setup-<ver>.exe.sha256   checksum the app checks before it runs a downloaded update
# The version is the app's <Version> in tools\pc_app\PcsApp.csproj: bump it for every release.
# Publish with publish.ps1 (GitHub release with both files).
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$tools = Split-Path $here
$out = Join-Path $here "out"
$work = Join-Path $out "work"
$pay = Join-Path $work "payload"

$pawn = Join-Path $here "vendor\PawnIO_setup.exe"
if (-not (Test-Path $pawn)) { throw "vendor\PawnIO_setup.exe missing (official PawnIO.Setup release)" }
$sig = Get-AuthenticodeSignature $pawn
if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch "CN=namazso\.eu") { throw "PawnIO_setup.exe signature not valid" }
$ver = (Select-Xml -Path (Join-Path $tools "pc_app\PcsApp.csproj") -XPath "//Version").Node.InnerText

Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $pay | Out-Null

Write-Host "1/6 app $ver (PCS.exe, self-contained)"
dotnet publish (Join-Path $tools "pc_app\PcsApp.csproj") -c Release -o (Join-Path $pay "app") -nologo -v q | Out-Host
Get-ChildItem (Join-Path $pay "app") -Include *.pdb, *.xml -Recurse | Remove-Item -Force

Write-Host "2/6 sensor reader (self-contained)"
dotnet publish (Join-Path $tools "pc_sensors\PcsSensors.csproj") -c Release -r win-x64 --self-contained true `
    -o (Join-Path $pay "sensors") -nologo -v q | Out-Host
Get-ChildItem (Join-Path $pay "sensors") -Include *.pdb -Recurse | Remove-Item -Force

Write-Host "3/6 service (PyInstaller)"
$dash = Join-Path $tools "pc_dashboard"
$pyArgs = @("-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--noconsole", "--name", "PcsService",
    "--icon", (Join-Path $tools "pc_app\pcs.ico"), "--distpath", (Join-Path $work "dist"), "--workpath", (Join-Path $work "build"),
    "--specpath", $work, "--exclude-module", "tkinter")
foreach ($f in Get-ChildItem $dash -File | Where-Object { $_.Extension -in ".html", ".js", ".css" }) { $pyArgs += @("--add-data", "$($f.FullName);.") }
$pyArgs += @("--add-data", "$dash\boards\*.svg;boards")
foreach ($m in "sensors_src", "events_watch", "analyzer", "procs", "cper", "hwinfo", "hwinfo_out", "fast_rules", "daily", "pcs_prog", "pcs_paths", "diag") {
    $pyArgs += @("--hidden-import", $m)
}
$pyArgs += (Join-Path $dash "pcs_dash.py")
Push-Location $dash
$ErrorActionPreference = "Continue"          # PyInstaller logs on stderr; PowerShell 5.1 would stop on it
& py @pyArgs *> (Join-Path $work "pyinstaller.log")
$code = $LASTEXITCODE
$ErrorActionPreference = "Stop"
Pop-Location
if ($code -ne 0) { throw "PyInstaller failed ($code): see $work\pyinstaller.log" }
Copy-Item (Join-Path $work "dist\PcsService") (Join-Path $pay "service") -Recurse

Write-Host "4/6 uninstaller"
$setupProj = Join-Path $tools "pc_setup\PcsSetup.csproj"
dotnet build $setupProj -c Release -o (Join-Path $work "uninst") -p:Version=$ver -nologo -v q | Out-Host
Copy-Item (Join-Path $work "uninst\PcsSetup.exe") (Join-Path $pay "app\Uninstall PCS.exe")
Copy-Item $pawn $pay
$rel = Join-Path $here "release.json"
if (Test-Path $rel) {
    $repo = (Get-Content $rel -Raw | ConvertFrom-Json).repo
    if ($repo -and $repo -notmatch "OWNER") { @{ repo = $repo } | ConvertTo-Json | Set-Content (Join-Path $pay "update.json") -Encoding ascii; Write-Host "   updates from github.com/$repo" }
    else { Write-Host "   release.json has no repo yet: this build will not look for updates" -ForegroundColor Yellow }
} else { Write-Host "   no release.json: this build will not look for updates" -ForegroundColor Yellow }

Write-Host "5/6 payload"
$zip = Join-Path $work "payload.zip"
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::CreateFromDirectory($pay, $zip, [IO.Compression.CompressionLevel]::Optimal, $false)

Write-Host "6/6 setup"
# obj\ is shared with the uninstaller build: a clean restore so the payload resource is picked up
Remove-Item (Join-Path $tools "pc_setup\obj") -Recurse -Force -ErrorAction SilentlyContinue
dotnet build $setupProj -c Release -o (Join-Path $work "setup") -p:Version=$ver -p:Payload=$zip -nologo -v q | Out-Host
$exe = Join-Path $out "PCS-Setup-$ver.exe"
Copy-Item (Join-Path $work "setup\PcsSetup.exe") $exe -Force
$hash = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLower()
"$hash  PCS-Setup-$ver.exe" | Set-Content "$exe.sha256" -Encoding ascii -NoNewline
Remove-Item (Join-Path $tools "pc_setup\obj") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $work -Recurse -Force
$mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host "Done: $exe ($mb MB), sha256 $hash" -ForegroundColor Green
