@echo off
setlocal
title LuxEdge Derleyici
pushd "%~dp0"
if errorlevel 1 exit /b 1

echo LuxEdge backend ve Windows kurulum paketi derleniyor...
echo.
set "BUILD_SHELL=powershell.exe"
where /q pwsh.exe
if not errorlevel 1 set "BUILD_SHELL=pwsh.exe"
"%BUILD_SHELL%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build-windows.ps1" -Prepare
set "BUILD_RESULT=%ERRORLEVEL%"
echo.
if "%BUILD_RESULT%"=="0" (
    echo BASARILI: Kuruluma hazir program dist klasorunde.
) else (
    echo HATA: Derleme tamamlanamadi. Yukaridaki hata mesajini kontrol edin.
    echo Onceki bir kurulum dosyasini yeni derleme olarak kullanmayin.
)
popd
if /I not "%~1"=="--no-pause" pause
exit /b %BUILD_RESULT%
