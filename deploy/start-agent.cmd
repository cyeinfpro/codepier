@echo off
cd /d "%~dp0.."
set "codepier_base=%USERPROFILE%\.codepier-agent"
if not exist "%codepier_base%" if exist "%USERPROFILE%\.remote-dev-agent" set "codepier_base=%USERPROFILE%\.remote-dev-agent"
if exist "%codepier_base%\runtime\agent\service_watchdog.py" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "deploy\install-from-hub.ps1" -Action start -InstallDir "%codepier_base%"
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "deploy\install-agent.ps1"
)
set "agent_exit_code=%errorlevel%"
if not "%agent_exit_code%"=="0" pause
exit /b %agent_exit_code%
