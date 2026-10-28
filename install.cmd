@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -InstallPrerequisites
set "CODE=%ERRORLEVEL%"
echo.
if not "%CODE%"=="0" echo Installation failed. See the messages above.
pause
exit /b %CODE%