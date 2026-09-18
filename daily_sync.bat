@echo off
cd /d "%~dp0"
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8
if not exist data mkdir data
echo ===== %date% %time% =====
echo ===== %date% %time% =====>> data\daily_sync.log
echo.
echo Dang chay daily_sync.py
echo Log: data\daily_sync.log
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "python -u daily_sync.py @args 2>&1 | Tee-Object -FilePath 'data\daily_sync.log' -Append; exit $LASTEXITCODE" -- %*
set EXITCODE=%ERRORLEVEL%
echo.
echo Exit %EXITCODE%
echo Exit %EXITCODE%>> data\daily_sync.log
if %EXITCODE% neq 0 (echo THAT BAI.) else (echo XONG.)
echo.
pause
exit /b %EXITCODE%
