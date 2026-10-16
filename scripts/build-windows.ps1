param([string]$Python = '', [switch]$Prepare)
$ErrorActionPreference = 'Stop'
function Test-Python([string]$Executable) {
    try {
        & $Executable -c 'import sys' 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}
function Get-Sha256([string]$File) {
    $stream = [System.IO.File]::OpenRead((Join-Path $projectRoot $File))
    $hasher = [System.Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-', '') }
    finally { $stream.Dispose(); $hasher.Dispose() }
}
$projectRoot = Split-Path $PSScriptRoot -Parent
Push-Location $projectRoot
try {
    if (-not (Get-Command node.exe -ErrorAction SilentlyContinue) -or
        -not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
        throw 'Node.js ve npm bulunamadi. Node.js kurup derleyiciyi yeniden calistirin.'
    }
    if (-not $Python) {
        $localPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
        if ((Test-Path -LiteralPath $localPython) -and (Test-Python $localPython)) {
            $Python = $localPython
        } elseif ((Get-Command python.exe -ErrorAction SilentlyContinue) -and (Test-Python 'python.exe')) {
            $Python = 'python.exe'
        } elseif (Get-Command py.exe -ErrorAction SilentlyContinue) {
            $Python = & py.exe -3 -c 'import sys; print(sys.executable)'
            if ($LASTEXITCODE -ne 0) { throw 'Python 3 bulunamadi.' }
        } else {
            throw 'Python 3 bulunamadi. Python kurup derleyiciyi yeniden calistirin.'
        }
    }
    if ($Prepare) {
        Write-Host '[1/4] Derleme bagimliliklari kontrol ediliyor...'
        & $Python -c "import importlib.util, sys; sys.exit(0 if all(importlib.util.find_spec(m) is not None for m in 'mss numpy PIL pystray PyInstaller'.split()) else 1)"
        if ($LASTEXITCODE -ne 0) {
            # Missing dependencies are installed only into the project environment.
            $localPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
            if (-not ((Test-Path -LiteralPath $localPython) -and (Test-Python $localPython))) {
                & $Python -m venv (Join-Path $projectRoot '.venv')
                if ($LASTEXITCODE -ne 0) { throw 'Proje Python ortami olusturulamadi.' }
            }
            $Python = $localPython
            & $Python -m pip install -r requirements.txt pyinstaller
            if ($LASTEXITCODE -ne 0) { throw 'Python bagimliliklari yuklenemedi.' }
        }
        if (-not (Test-Path -LiteralPath 'node_modules\.bin\electron-builder.cmd') -or
            -not (Test-Path -LiteralPath 'node_modules\electron\dist\electron.exe')) {
            & npm.cmd ci
            if ($LASTEXITCODE -ne 0) { throw 'Electron bagimliliklari yuklenemedi.' }
        }
    }
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $projectRoot 'build\pyinstaller-cache'
    $env:ELECTRON_BUILDER_CACHE = Join-Path $projectRoot 'build\electron-builder-cache'
    Write-Host '[2/4] Guncel Python backend derleniyor...'
    & $Python -m PyInstaller --onefile --noconsole --name lush_backend --clean --noconfirm --noupx --distpath dist\backend --workpath build\backend --specpath build ambilight_pc.py
    if ($LASTEXITCODE -ne 0) { throw 'Backend build failed' }
    Copy-Item -LiteralPath 'dist\backend\lush_backend.exe' -Destination 'lush_backend.exe' -Force
    Write-Host '[3/4] Electron ve Windows kurulum paketi derleniyor...'
    & .\node_modules\.bin\electron-builder.cmd --win --x64 --config.electronDist=node_modules/electron/dist
    if ($LASTEXITCODE -ne 0) { throw 'Windows packaging failed' }
    Write-Host '[4/4] Paket icerigi dogrulaniyor...'
    $builtHash = Get-Sha256 'lush_backend.exe'
    $packagedHash = Get-Sha256 'dist\win-unpacked\resources\lush_backend.exe'
    if ($builtHash -ne $packagedHash) { throw 'Packaged backend hash mismatch' }
    if (Test-Path -LiteralPath 'dist\win-unpacked\resources\lush_backend') { throw 'Unexpected Linux backend in Windows package' }
    & $Python scripts/verify-package.py
    if ($LASTEXITCODE -ne 0) { throw 'Backend kaynak dogrulamasi basarisiz.' }
    & node.exe scripts/verify-asar.js
    if ($LASTEXITCODE -ne 0) { throw 'Arayuz paket dogrulamasi basarisiz.' }
    $version = (Get-Content -LiteralPath 'package.json' -Raw | ConvertFrom-Json).luxedgeVersion
    Get-Item -LiteralPath "dist\LuxEdge Setup $version.exe" | Select-Object FullName, Length
    Write-Output "Verified backend SHA256: $builtHash"
} finally {
    Pop-Location
}
