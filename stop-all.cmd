@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\stop-backend.ps1" -IncludeMaiBot
if errorlevel 1 pause