param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
Push-Location $projectRoot
try {
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $projectRoot 'build\pyinstaller-cache'
    $env:ELECTRON_BUILDER_CACHE = Join-Path $projectRoot 'build\electron-builder-cache'
    & $Python -m PyInstaller --onefile --noconsole --name lush_backend --clean --noconfirm --noupx --distpath dist\backend --workpath build\backend --specpath build ambilight_pc.py
    if ($LASTEXITCODE -ne 0) { throw 'Backend build failed' }
    Copy-Item -LiteralPath 'dist\backend\lush_backend.exe' -Destination 'lush_backend.exe' -Force
    & .\node_modules\.bin\electron-builder.cmd --win --x64 --config.electronDist=node_modules/electron/dist
    if ($LASTEXITCODE -ne 0) { throw 'Windows packaging failed' }
    $builtHash = (Get-FileHash -LiteralPath 'lush_backend.exe' -Algorithm SHA256).Hash
    $packagedHash = (Get-FileHash -LiteralPath 'dist\win-unpacked\resources\lush_backend.exe' -Algorithm SHA256).Hash
    if ($builtHash -ne $packagedHash) { throw 'Packaged backend hash mismatch' }
    if (Test-Path -LiteralPath 'dist\win-unpacked\resources\lush_backend') { throw 'Unexpected Linux backend in Windows package' }
    $version = (Get-Content -LiteralPath 'package.json' -Raw | ConvertFrom-Json).luxedgeVersion
    Get-Item -LiteralPath "dist\LuxEdge Setup $version.exe" | Select-Object FullName, Length
    Write-Output "Verified backend SHA256: $builtHash"
} finally {
    Pop-Location
}
