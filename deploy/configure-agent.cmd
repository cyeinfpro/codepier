@echo off
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Run install-agent.ps1 first.
  pause
  exit /b 1
)
rem The Python CLI prompts for the new URL; do not interpolate untrusted cmd input.
".venv\Scripts\python.exe" -m agent configure
set "agent_exit_code=%errorlevel%"
pause
exit /b %agent_exit_code%
