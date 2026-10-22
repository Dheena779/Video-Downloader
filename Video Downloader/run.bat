@echo off
title OmniStream - Video Stream Downloader
cd /d "%~dp0"
echo Starting OmniStream Desktop Application...
python app.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Application exited with an error.
    pause
)
