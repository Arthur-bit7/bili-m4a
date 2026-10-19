@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto failed
)
if not exist ".venv\installed.ok" (
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto failed
  echo installed>".venv\installed.ok"
)
".venv\Scripts\python.exe" app.py
if errorlevel 1 goto failed
exit /b 0
:failed
echo.
echo Startup failed. Install Python 3.10+ with "Add Python to PATH" enabled.
echo Check the error above and your Internet connection. See README.md.
pause
exit /b 1
