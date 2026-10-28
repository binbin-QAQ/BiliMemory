@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\start-backend.ps1" -StartMaiBot -WithQQ -WithBiliDM -OpenPanel
if errorlevel 1 pause