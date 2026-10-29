@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -B run_daily_export.py %*
) else (
  python -B run_daily_export.py %*
)
pause
