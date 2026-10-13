@echo off
cd /d "%~dp0"
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8

if not exist "logs" mkdir logs
set DATE_TAG=%date:~10,4%%date:~4,2%%date:~7,2%_%time:~0,2%%time:~3,2%%time:~6,2%
set DATE_TAG=%DATE_TAG: =0%
set LOG_FILE=logs\cron_pipeline_%DATE_TAG%.log

echo ================================================== >> %LOG_FILE%
echo [START] Bat dau chay chu ky cron UpproInfo: %date% %time% >> %LOG_FILE%
echo ================================================== >> %LOG_FILE%

echo [1/4] Chay daily_sync.py --fresh >> %LOG_FILE%
python daily_sync.py --fresh >> %LOG_FILE% 2>&1

echo [2/4] Chay refresh_details.py --fresh >> %LOG_FILE%
python refresh_details.py --fresh >> %LOG_FILE% 2>&1

echo [3/4] Chay scan_metrics.py --fresh >> %LOG_FILE%
python scan_metrics.py --fresh >> %LOG_FILE% 2>&1

echo [4/4] Chay refresh_search_ads_brands.py --fresh >> %LOG_FILE%
python refresh_search_ads_brands.py --fresh >> %LOG_FILE% 2>&1

echo ================================================== >> %LOG_FILE%
echo [DONE] Hoan tat tat ca cac buoc: %date% %time% >> %LOG_FILE%
echo ================================================== >> %LOG_FILE%
