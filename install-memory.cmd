@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\install-memory.ps1"
set "CODE=%ERRORLEVEL%"
echo.
if not "%CODE%"=="0" echo A-Memorix installation failed. See the messages above.
pause
exit /b %CODE%