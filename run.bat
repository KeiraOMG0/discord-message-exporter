@echo off
rem Double-click to open the exporter. Extra args are passed through, e.g.  run.bat --list
cd /d "%~dp0"
if not exist venv\Scripts\python.exe (
    echo First run: setting up venv...
    python -m venv venv || goto :fail
    venv\Scripts\python.exe -m pip install -q -r requirements.txt || goto :fail
)
venv\Scripts\python.exe scraper.py %*
if errorlevel 1 goto :fail
exit /b 0
:fail
pause
exit /b 1
