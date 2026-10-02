$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$repoUrl = 'https://github.com/Jude-A/lucidadl.git'
$revision = 'bf3fa1eb87634b3f60bea787c222351c82ec2f16'
$vendorPath = Join-Path $PSScriptRoot 'vendor\lucidadl'
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
Write-Host 'Installing the community lucidadl client into this folder.'
Write-Host 'Source: https://github.com/Jude-A/lucidadl (not affiliated with lucida.to)'
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Git is required.' }
if (-not (Test-Path -LiteralPath $vendorPath)) {
    New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot 'vendor') | Out-Null
    & git clone --no-checkout $repoUrl $vendorPath
    if ($LASTEXITCODE -ne 0) { throw 'Source download failed.' }
    & git -C $vendorPath checkout --detach $revision
    if ($LASTEXITCODE -ne 0) { throw 'Could not select the pinned source revision.' }
}
$origin = & git -C $vendorPath remote get-url origin
if ($LASTEXITCODE -ne 0 -or $origin -ne $repoUrl) { throw 'Existing source folder has an unexpected origin.' }
$head = & git -C $vendorPath rev-parse HEAD
if ($LASTEXITCODE -ne 0 -or $head -ne $revision) { throw 'Existing source folder has a different revision. It has been left untouched.' }
$changes = & git -C $vendorPath status --porcelain
if ($changes) { throw 'The client source has local changes. It has been left untouched.' }
if (-not (Test-Path -LiteralPath $pythonPath)) {
    & python -m venv (Join-Path $PSScriptRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.10 or newer is required, with tkinter and venv support.' }
}
& $pythonPath -m pip install $vendorPath
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $pythonPath -m pip install 'tidalapi==0.8.11'
if ($LASTEXITCODE -ne 0) { throw 'TIDAL account dependency installation failed.' }
& $pythonPath -m pip install 'yt-dlp==2026.8.19'
if ($LASTEXITCODE -ne 0) { throw 'SoundCloud dependency installation failed.' }
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PSScriptRoot '.local\browsers'
& $pythonPath -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw 'Chromium installation failed. Run this installer again to retry.' }
& $pythonPath -m pip freeze | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'installed-packages.txt') -Encoding utf8
Write-Host 'Installed. Open FRipper. SoundCloud links are ready; use Connect TIDAL for your account or Set up Lucida for Lucida sources.'
