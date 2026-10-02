@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if errorlevel 1 (
  echo Installation failed. The error is shown above.
  pause
  exit /b 1
)
echo Ready. Double-click Launch Lucida.vbs to open the app.
pause
