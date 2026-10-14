@echo off
rem Double-click to start DelTool. The first start installs everything it needs.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 run.py %*
) else (
  python run.py %*
)
if errorlevel 1 (
  echo.
  echo DelTool stopped with an error. Python 3.10+ is required: https://www.python.org/downloads/
  pause
)
