@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run start.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install --upgrade -r requirements.txt
pause
