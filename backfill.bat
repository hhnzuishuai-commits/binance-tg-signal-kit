@echo off
chcp 65001 >nul
cd /d "%~dp0"
set START=2026-10-10
set END=2026-10-16
set /p START=Start date [%START%]: 
set /p END=End date [%END%]: 
echo Exporting %START% .. %END% (about 1 minute per day, keep this window open)
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -B auto_run.py --mode backfill --start %START% --end %END%
) else (
  python -B auto_run.py --mode backfill --start %START% --end %END%
)
echo.
echo Done. Log: %LOCALAPPDATA%\trading-review\auto_run.log
pause
