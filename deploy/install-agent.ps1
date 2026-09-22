param([string]$PairingFile, [string]$AllowRoot)
$ErrorActionPreference = 'Stop'
$InputDirectory = (Get-Location).Path
$SourceDirectory = Split-Path $PSScriptRoot -Parent
$InstallDirectory = Join-Path $env:USERPROFILE '.codepier-agent'
if (-not (Test-Path -LiteralPath $InstallDirectory) -and (Test-Path -LiteralPath (Join-Path $env:USERPROFILE '.remote-dev-agent'))) {
  $InstallDirectory = Join-Path $env:USERPROFILE '.remote-dev-agent'
}
$CodePierPython = Join-Path $InstallDirectory 'runtime/.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $CodePierPython)) { $CodePierPython = (Get-Command python -ErrorAction Stop).Source }
& $CodePierPython -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 'Python 3.12+ required')"
if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.12+ and add it to PATH first.' }
if (-not (Test-Path -LiteralPath (Join-Path $InstallDirectory 'config.json'))) {
  if (-not $PairingFile) { $PairingFile = Read-Host 'Pairing JSON full path' }
  if (-not $AllowRoot) { $AllowRoot = Read-Host 'Allowed project parent directory, e.g. D:\Projects' }
  if (-not $PairingFile -or -not $AllowRoot) { throw 'Pairing file and allowed directory are required' }
}
$CodePierArgs = @((Join-Path $SourceDirectory 'scripts/install_agent.py'), '--source', $SourceDirectory)
if ($PairingFile) {
  if (-not [IO.Path]::IsPathRooted($PairingFile) -and -not $PairingFile.StartsWith('~')) { $PairingFile = Join-Path $InputDirectory $PairingFile }
  $CodePierArgs += @('--pairing-file', $PairingFile)
}
if ($AllowRoot) {
  if (-not [IO.Path]::IsPathRooted($AllowRoot) -and -not $AllowRoot.StartsWith('~')) { $AllowRoot = Join-Path $InputDirectory $AllowRoot }
  $CodePierArgs += @('--allow', $AllowRoot)
}
# The source installer may replace runtime, so use the external interpreter.
$CodePierPython = (& $CodePierPython -c 'import sys; print(getattr(sys, "_base_executable", None) or sys.executable)' | Select-Object -Last 1)
if ($LASTEXITCODE -ne 0 -or -not $CodePierPython) { throw 'No external Python interpreter found' }
& $CodePierPython @CodePierArgs
if ($LASTEXITCODE -ne 0) { throw 'Background Agent setup failed; inspect the error above. Existing configuration was preserved.' }
Write-Host 'Agent runs in the background, starts before desktop login, and recovers automatically. You can close this window.'
