@echo off
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Run install-agent.ps1 first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m agent run
set "agent_exit_code=%errorlevel%"
pause
exit /b %agent_exit_code%
