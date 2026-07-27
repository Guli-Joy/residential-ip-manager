@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"

set "NEED_SETUP=0"
if not exist ".venv\Scripts\pythonw.exe" set "NEED_SETUP=1"
if "%NEED_SETUP%"=="0" (
  ".venv\Scripts\python.exe" -c "import httpx, PySide6, yaml, residential_ip_manager" >nul 2>&1
  if errorlevel 1 set "NEED_SETUP=1"
)

if "%NEED_SETUP%"=="1" (
  if not exist "%LOCALAPPDATA%\ResidentialIPManager" mkdir "%LOCALAPPDATA%\ResidentialIPManager" >nul 2>&1
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1" > "%LOCALAPPDATA%\ResidentialIPManager\setup.log" 2>&1
  if errorlevel 1 (
    echo Setup failed. See %LOCALAPPDATA%\ResidentialIPManager\setup.log
    exit /b 1
  )
)

start "Residential IP Manager" ".venv\Scripts\pythonw.exe" -m residential_ip_manager.main %*
exit /b 0
