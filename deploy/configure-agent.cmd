@echo off
cd /d "%~dp0.."
set "codepier_base=%USERPROFILE%\.codepier-agent"
if not exist "%codepier_base%" if exist "%USERPROFILE%\.remote-dev-agent" set "codepier_base=%USERPROFILE%\.remote-dev-agent"
set "codepier_python=%codepier_base%\runtime\.venv\Scripts\python.exe"
if not exist "%codepier_python%" set "codepier_python=%CD%\.venv\Scripts\python.exe"
if not exist "%codepier_python%" (
  echo Run install-agent.ps1 first.
  pause
  exit /b 1
)
rem The Python CLI prompts for the new URL; do not interpolate untrusted cmd input.
"%codepier_python%" -m agent --config "%codepier_base%\config.json" configure
set "agent_exit_code=%errorlevel%"
pause
exit /b %agent_exit_code%
