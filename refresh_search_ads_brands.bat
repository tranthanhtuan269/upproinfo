@echo off
cd /d "%~dp0"
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8
echo ===== %date% %time% =====>> data\refresh_ads.log
python refresh_search_ads_brands.py %* >> data\refresh_ads.log 2>&1
echo Exit %ERRORLEVEL%>> data\refresh_ads.log
