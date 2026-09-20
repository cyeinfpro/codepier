param([string]$PairingFile, [string]$AllowRoot)
$ErrorActionPreference = 'Stop'
$InputDirectory = (Get-Location).Path
Set-Location (Join-Path $PSScriptRoot '..')
python -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 'Python 3.12+ required')"
if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.12+ and add it to PATH first.' }
python -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
& .\.venv\Scripts\python.exe -m pip install -r requirements-agent.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
if (-not $PairingFile) { $PairingFile = Read-Host 'Pairing JSON full path' }
if (-not $AllowRoot) { $AllowRoot = Read-Host 'Allowed project parent directory, e.g. D:\Projects' }
if (-not $PairingFile -or -not $AllowRoot) { throw 'Pairing file and allowed directory are required' }
if (-not [System.IO.Path]::IsPathRooted($PairingFile) -and -not $PairingFile.StartsWith('~')) { $PairingFile = Join-Path $InputDirectory $PairingFile }
if (-not [System.IO.Path]::IsPathRooted($AllowRoot) -and -not $AllowRoot.StartsWith('~')) { $AllowRoot = Join-Path $InputDirectory $AllowRoot }
& .\.venv\Scripts\python.exe -m agent init --pairing-file $PairingFile --allow $AllowRoot
if ($LASTEXITCODE -ne 0) { throw 'Agent initialization failed' }
Write-Host 'Start with deploy\start-agent.cmd. Change IP/port with deploy\configure-agent.cmd.'
Write-Host 'Restrict the NTFS permissions on your .codepier-agent folder to your own Windows account.'
