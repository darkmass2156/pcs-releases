# Publishes a built PCS setup as a GitHub release (2026-11-23). Installed copies find it within 6 h (or at their next
# start + 1 min) and offer the update.
#   powershell -ExecutionPolicy Bypass -File tools\standalone\publish.ps1 -Notes "What changed"
# Uses the GitHub CLI (gh auth login once). Repo from release.json; version from tools\pc_app\PcsApp.csproj; files from
# out\ (build.ps1 first). The tag is v<version>; an existing tag is refused (bump the version instead).
param([string]$Notes = "", [switch]$Draft)
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$repo = (Get-Content (Join-Path $here "release.json") -Raw | ConvertFrom-Json).repo
if (-not $repo -or $repo -match "OWNER") { throw "set the releases repo in tools\standalone\release.json" }
$ver = (Select-Xml -Path (Join-Path (Split-Path $here) "pc_app\PcsApp.csproj") -XPath "//Version").Node.InnerText
$exe = Join-Path $here "out\PCS-Setup-$ver.exe"
$sha = "$exe.sha256"
if (-not (Test-Path $exe) -or -not (Test-Path $sha)) { throw "out\PCS-Setup-$ver.exe (+ .sha256) missing: run build.ps1" }
$ErrorActionPreference = "Continue"
gh release view "v$ver" --repo $repo *> $null
if ($LASTEXITCODE -eq 0) { throw "release v$ver already exists in ${repo}: bump the version" }
$args_ = @("release", "create", "v$ver", $exe, $sha, "--repo", $repo, "--title", "PCS $ver", "--notes", ($(if ($Notes) { $Notes } else { "PCS $ver" })))
if ($Draft) { $args_ += "--draft" }
gh @args_
if ($LASTEXITCODE -ne 0) { throw "gh release create failed" }
Write-Host "Published PCS $ver to github.com/$repo" -ForegroundColor Green
