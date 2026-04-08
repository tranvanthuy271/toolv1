@echo off
setlocal
cd /d "%~dp0"

set "NO_PAUSE="
if /I "%~1"=="--no-pause" set "NO_PAUSE=1"

set "PYTHON_EXE=python"
if exist ".venv\Scripts\python.exe" set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
	"$target = Join-Path (Get-Location) 'dist\WeaponSpriteAdapter.exe';" ^
	"if (Test-Path $target) {" ^
	"  $running = @(Get-Process WeaponSpriteAdapter -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $target });" ^
	"  if ($running.Count -gt 0) {" ^
	"    Write-Host 'Stopping running dist\WeaponSpriteAdapter.exe before build...';" ^
	"    $running | Stop-Process -Force;" ^
	"    $running | ForEach-Object { Wait-Process -Id $_.Id -Timeout 5 -ErrorAction SilentlyContinue };" ^
	"  }" ^
	"  for ($attempt = 0; $attempt -lt 20; $attempt++) {" ^
	"    try { Remove-Item $target -Force -ErrorAction Stop; Write-Host 'Removed previous dist\WeaponSpriteAdapter.exe before build.'; break }" ^
	"    catch { if ($attempt -eq 19) { Write-Error ('Cannot remove locked file: ' + $target); exit 1 }; Start-Sleep -Milliseconds 250 }" ^
	"  }" ^
	"}"
if errorlevel 1 goto :error

"%PYTHON_EXE%" -m pip install -r requirements-build.txt
if errorlevel 1 goto :error

"%PYTHON_EXE%" -m PyInstaller --noconsole --clean --onefile --collect-all tkinterdnd2 --name WeaponSpriteAdapter app.py
if errorlevel 1 goto :error

echo.
echo Build complete.
echo EXE: dist\WeaponSpriteAdapter.exe
if not defined NO_PAUSE pause
exit /b 0

:error
echo.
echo Build failed.
if not defined NO_PAUSE pause
exit /b 1
