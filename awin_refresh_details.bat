@echo off
cd /d "%~dp0"
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8
python -u awin_refresh_details.py %*
exit /b %ERRORLEVEL%
