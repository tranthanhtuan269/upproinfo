@echo off
cd /d "%~dp0"
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8
echo ===== %date% %time% =====>> data\refresh_details.log
python refresh_details.py %* >> data\refresh_details.log 2>&1
echo Exit %ERRORLEVEL%>> data\refresh_details.log
