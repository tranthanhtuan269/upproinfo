@echo off
cd /d "%~dp0"
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8
python -u awin_daily_sync.py %*
exit /b %ERRORLEVEL%
