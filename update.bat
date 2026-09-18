@echo off
cd /d "%~dp0"
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8
echo.
echo === Backup server + day catalog local len VPS ===
echo Khong de billing.db / tai khoan thanh toan.
echo.
python update_data.py
echo.
if errorlevel 1 (
  echo THAT BAI.
) else (
  echo XONG.
)
pause
