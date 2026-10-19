@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "runtime\TaxZero.exe" (
  start "" "%~dp0runtime\TaxZero.exe" --bridge
  exit /b 0
)
if exist "app.py" (
  where python >nul 2>nul
  if not errorlevel 1 goto source
)
echo 未找到完整 Windows 运行组件。请完整解压 Windows 执行端后再打开此文件。
pause
exit /b 1
:source
python -X utf8 "%~dp0app.py" --bridge
exit /b %errorlevel%
