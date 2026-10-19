@echo off
cd /d "%~dp0"
set "PY="
for /f "delims=" %%I in ('where python 2^>nul') do (
  echo %%I | findstr /I "WindowsApps" >nul
  if errorlevel 1 (
    if not defined PY set "PY=%%I"
  )
)
if not defined PY (
  echo Python 3 was not found. Install Python 3.12 and run this file again.
  pause
  exit /b 1
)
"%PY%" -c "import PIL" 2>nul
if errorlevel 1 (
  echo Installing dependencies...
  "%PY%" -m pip install -r "%~dp0requirements.txt"
  if errorlevel 1 (
    echo Dependency install failed.
    pause
    exit /b 1
  )
)
"%PY%" -c "import wxauto4" 2>nul
if errorlevel 1 (
  "%PY%" -c "import wxauto" 2>nul
  if errorlevel 1 (
    echo Installing dependencies...
    "%PY%" -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 (
      echo Dependency install failed.
      pause
      exit /b 1
    )
  )
)
set "PYW=%PY:python.exe=pythonw.exe%"
if exist "%PYW%" (
  start "" "%PYW%" "%~dp0app.py"
) else (
  start "" "%PY%" "%~dp0app.py"
)
