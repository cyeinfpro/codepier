param([string]$PairingFile, [string]$AllowRoot, [ValidateSet('full','disabled')][string]$Shell = 'full')
$ErrorActionPreference = 'Stop'
$InputDirectory = (Get-Location).Path
$SourceDirectory = Split-Path $PSScriptRoot -Parent
$InstallDirectory = Join-Path $env:USERPROFILE '.codepier-agent'
if (-not (Test-Path -LiteralPath $InstallDirectory) -and (Test-Path -LiteralPath (Join-Path $env:USERPROFILE '.remote-dev-agent'))) {
  $InstallDirectory = Join-Path $env:USERPROFILE '.remote-dev-agent'
}
$CodePierPython = Join-Path $InstallDirectory 'runtime/.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $CodePierPython)) { $CodePierPython = (Get-Command python -ErrorAction Stop).Source }
& $CodePierPython -c "import sys; sys.exit(0 if sys.version_info >= (3,13) else '需要 Python 3.13 或更高版本')"
if ($LASTEXITCODE -ne 0) { throw '请先安装 Python 3.13 或更高版本并加入 PATH。' }
if (-not (Test-Path -LiteralPath (Join-Path $InstallDirectory 'config.json'))) {
  if (-not $PairingFile) { $PairingFile = Read-Host '配对 JSON 文件完整路径' }
  if (-not $AllowRoot) { $AllowRoot = Read-Host '允许访问的项目父目录，例如 D:\Projects' }
  if (-not $PairingFile -or -not $AllowRoot) { throw '配对文件和授权目录不能为空' }
}
if (Test-Path -LiteralPath (Join-Path $InstallDirectory 'config.json')) { Write-Host '检测到已有配置：保留原权限。' }
elseif ($Shell -eq 'full') { Write-Host '权限范围：开启当前系统用户完整 Shell 和目录任务，不是目录沙箱；可用 -Shell disabled 关闭。' }
else { Write-Host '权限范围：关闭 Shell 和目录任务；目录文件访问仍按授权配置。' }
$CodePierArgs = @((Join-Path $SourceDirectory 'scripts/install_agent.py'), '--source', $SourceDirectory, '--shell', $Shell)
if ($PairingFile) {
  if (-not [IO.Path]::IsPathRooted($PairingFile) -and -not $PairingFile.StartsWith('~')) { $PairingFile = Join-Path $InputDirectory $PairingFile }
  $CodePierArgs += @('--pairing-file', $PairingFile)
}
if ($AllowRoot) {
  if (-not [IO.Path]::IsPathRooted($AllowRoot) -and -not $AllowRoot.StartsWith('~')) { $AllowRoot = Join-Path $InputDirectory $AllowRoot }
  $CodePierArgs += @('--allow', $AllowRoot)
}
# The source installer may replace runtime, so use the external interpreter.
$CodePierPython = (& $CodePierPython -c "import sys; print(getattr(sys, '_base_executable', None) or sys.executable)" | Select-Object -Last 1)
if ($LASTEXITCODE -ne 0 -or -not $CodePierPython) { throw '未找到运行时目录之外的 Python 解释器，未继续安装' }
& $CodePierPython @CodePierArgs
if ($LASTEXITCODE -ne 0) { throw '安装未完整完成；请查看上方失败阶段并用 status 核实配置、运行时与服务状态，勿重复配对。' }
Write-Host '本机安装流程完成。后台服务配置与 Hub 在线是不同状态，请在面板核实最近心跳。'
