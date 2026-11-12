# One-step setup for a fresh clone (Windows 10/11).
#   - Python environment in .venv with emcosim installed
#   - KiCad (board checks) through winget
#   - ngspice (circuit check) into tools\ngspice
#   - patched QEMU for ESP32 firmware tests into tools\qemu, from this repository's GitHub release
# QEMU, KiCad and ngspice are separate programs that emcosim runs as processes; they are
# downloaded here, not shipped in this repository.
#
# Usage (from the repository folder):  scripts\setup.cmd      or
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 [-NoKicad]
param([switch]$NoKicad)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Root = Split-Path -Parent $PSScriptRoot
$Tools = Join-Path $Root 'tools'
$NgspiceVersion = '44.2'
New-Item -ItemType Directory -Force $Tools | Out-Null
function Say($m) { Write-Host "`n== $m" -ForegroundColor Cyan }
function Have($c) { [bool](Get-Command $c -ErrorAction SilentlyContinue) }
function Fetch($url, $out) {
  Write-Host "downloading $url"
  & curl.exe -fL --retry 3 -A 'Mozilla/5.0' -o $out $url
  if ($LASTEXITCODE -ne 0) { throw "download failed: $url" }
}

$Repo = $env:EMCOSIM_REPO
if (-not $Repo) {
  $url = try { (& git -C $Root remote get-url origin 2>$null) } catch { '' }
  if ($url -match 'github\.com[:/]([^/]+/[^/.]+?)(\.git)?/?$') { $Repo = $Matches[1] }
}

Say 'Python environment (.venv)'
$py = $null
foreach ($c in @(@('py', '-3.12'), @('py', '-3.11'), @('py', '-3.10'), @('python'))) {
  if (Have $c[0]) {
    $args0 = @($c | Select-Object -Skip 1)
    try { & $c[0] @args0 -c "import sys; sys.exit(sys.version_info < (3, 10))" 2>$null } catch { continue }
    if ($LASTEXITCODE -eq 0) { $py = $c; break }
  }
}
if (-not $py) {
  if (-not (Have 'winget')) { throw 'Python 3.10+ is needed: install it from https://www.python.org/downloads/' }
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
  $py = @('py', '-3.12')
}
$venvPy = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
  $pyArgs = @($py | Select-Object -Skip 1) + @('-m', 'venv', (Join-Path $Root '.venv'))
  & $py[0] @pyArgs
}
& $venvPy -m pip install -q --upgrade pip
& $venvPy -m pip install -q -e "$Root[step,app,dev]"
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }

if (-not $NoKicad) {
  Say 'KiCad'
  $kicad = & $venvPy -c "from emcosim import eda; print(eda.find_cli() or '')"
  if ($kicad) { Write-Host "found $kicad" }
  elseif (Have 'winget') {
    winget install -e --id KiCad.KiCad --accept-package-agreements --accept-source-agreements
  } else { Write-Host 'winget not found: install KiCad 9 from https://www.kicad.org/download/windows/' }
}

Say 'ngspice'
$ng = Join-Path $Tools 'ngspice'
if (Test-Path (Join-Path $ng 'Spice64\bin\ngspice_con.exe')) { Write-Host 'already in tools\ngspice' }
else {
  $arc = Join-Path $Tools "ngspice-${NgspiceVersion}_64.7z"
  Fetch "https://sourceforge.net/projects/ngspice/files/ng-spice-rework/$NgspiceVersion/ngspice-${NgspiceVersion}_64.7z/download" $arc
  New-Item -ItemType Directory -Force $ng | Out-Null
  & tar.exe -xf $arc -C $ng
  if ($LASTEXITCODE -ne 0) {
    $7z = @("$env:ProgramFiles\7-Zip\7z.exe", "${env:ProgramFiles(x86)}\7-Zip\7z.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $7z -and (Have 'winget')) {
      winget install -e --id 7zip.7zip --accept-package-agreements --accept-source-agreements
      $7z = "$env:ProgramFiles\7-Zip\7z.exe"
    }
    & $7z x -y "-o$ng" $arc | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'could not unpack ngspice' }
  }
  Remove-Item $arc
}

Say 'Patched QEMU (ESP32 firmware tests)'
$q = Join-Path $Tools 'qemu'
if (Test-Path (Join-Path $q 'qemu-system-xtensa.exe')) { Write-Host 'already in tools\qemu' }
elseif (-not $Repo) { Write-Host 'cannot tell which GitHub repository this clone came from; set EMCOSIM_REPO=owner/name and run again' }
else {
  $zip = Join-Path $Tools 'qemu-esp32-cosim-windows-x64.zip'
  try {
    Fetch "https://github.com/$Repo/releases/latest/download/qemu-esp32-cosim-windows-x64.zip" $zip
    if (Test-Path $q) { Remove-Item -Recurse -Force $q }
    Expand-Archive -Path $zip -DestinationPath $q
    Remove-Item $zip
  } catch {
    Write-Host "no prebuilt QEMU yet ($_). Firmware tests are skipped until it is there; the other tests work." -ForegroundColor Yellow
  }
}

Say 'Check'
& $venvPy -c @"
from emcosim import eda
from emcosim.toolpaths import find_ngspice
from emcosim.mcu.qemu import find_qemu
try:
    q = find_qemu()[0]
except RuntimeError:
    q = None
for name, v in (('KiCad', eda.find_cli()), ('ngspice', find_ngspice()), ('QEMU', q)):
    print(f'{name:8s} {v or "NOT FOUND"}')
"@
Write-Host "`nDone. Start the app: scripts\start.cmd  (opens in your browser)"
