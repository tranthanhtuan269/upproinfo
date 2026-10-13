@echo off
cd /d "%~dp0"
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8

echo ============================================================
echo   GOAFFPRO STORE METRICS SCANNER (AITDK + Google Ads)
echo ============================================================
echo.
python scan_metrics.py %*
echo.
pause
