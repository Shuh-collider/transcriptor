@echo off
rem Installs or updates Transcriptor in this folder. Only ASCII here: cmd.exe misreads UTF-8 batch files.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
pause
