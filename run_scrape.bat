@echo off
cd /d "%~dp0"
set PYTHONUNBUFFERED=1
python scrape_brands.py
pause
