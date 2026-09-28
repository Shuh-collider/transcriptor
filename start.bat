@echo off
rem Only ASCII in this file: cmd.exe misreads UTF-8 batch files with Cyrillic text.
rem Russian messages are printed by Python itself.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Python environment not found. Run in this folder: uv sync
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m transcriptor serve %*
if errorlevel 1 pause
