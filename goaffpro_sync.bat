@echo off
cd /d "%~dp0"
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8

echo ============================================================
echo   GOAFFPRO STORE SYNC TOOL
echo ============================================================
echo.
python goaffpro_sync.py %*
echo.
pause
