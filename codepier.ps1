$ErrorActionPreference = 'Stop'
$Component = if ($args.Count) { [string]$args[0] } else { 'help' }
$ForwardArgs = @($args | Select-Object -Skip 1)
if ($Component -in @('help','--help','-h')) {
  Write-Host 'CodePier · 码头'
  Write-Host 'AI 与本地代码对接、任务停靠的地方'
  Write-Host '.\codepier.ps1 hub <init|run|...>'
  Write-Host '.\codepier.ps1 agent <parameters and subcommand>'
  Write-Host '.\codepier.ps1 control <parameters>'
  return
}
$Module = switch ($Component) { 'hub' { 'hub' } 'agent' { 'agent' } 'control' { 'agent.codepier_control' } default { throw 'Unknown CodePier command' } }
$Python = if ($env:CODEPIER_PYTHON) { $env:CODEPIER_PYTHON } else { Join-Path $PSScriptRoot '.venv/Scripts/python.exe' }
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'Prepare .venv or set CODEPIER_PYTHON to an interpreter with the project dependencies.' }
Push-Location $PSScriptRoot
try {
  & $Python -m $Module @ForwardArgs
  if ($LASTEXITCODE -ne 0) { throw ('CodePier command failed with exit '+$LASTEXITCODE) }
} finally { Pop-Location }
