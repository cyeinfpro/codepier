param(
  [ValidateSet('install','upgrade','uninstall','status','start')][string]$Action = 'install',
  [string]$Hub,
  [string]$Token,
  [string]$Sha256,
  [string]$AllowRoot,
  [ValidateSet('full','disabled')][string]$Shell = 'full',
  [string]$InstallDir = (Join-Path $env:USERPROFILE '.codepier-agent'),
  [switch]$NoService,
  [switch]$Yes,
  [string]$ExpectedDevice,
  [ValidateRange(5,120)][int]$DownloadTimeoutSec = 30,
  [ValidateRange(1,5)][int]$DownloadAttempts = 4
)
$ErrorActionPreference = 'Stop'
if (-not $PSBoundParameters.ContainsKey('InstallDir')) {
  $LegacyDir = Join-Path $env:USERPROFILE '.remote-dev-agent'
  if ((Test-Path -LiteralPath $InstallDir) -and (Test-Path -LiteralPath $LegacyDir)) {
    $LegacyItem = Get-Item -LiteralPath $LegacyDir -Force
    if (-not ($LegacyItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -or [string]$LegacyItem.Target -ne $InstallDir) {
      throw 'Both CodePier and legacy installations exist; no merge attempted.'
    }
  }
  if (-not (Test-Path -LiteralPath $InstallDir) -and (Test-Path -LiteralPath $LegacyDir)) { $InstallDir = $LegacyDir }
}
if (Test-Path -LiteralPath $InstallDir) {
  $InstallItem = Get-Item -LiteralPath $InstallDir -Force
  if ($InstallItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
    $Canonical = Join-Path (Split-Path $InstallDir -Parent) '.codepier-agent'
    if ((Split-Path $InstallDir -Leaf) -ne '.remote-dev-agent' -or [string]$InstallItem.Target -ne $Canonical) { throw 'Unrecognized installation link' }
    $InstallDir = $Canonical
  }
}
if (-not [IO.Path]::IsPathRooted($InstallDir)) { throw 'Install directory must be absolute' }
if ($Action -eq 'uninstall' -and -not (Test-Path -LiteralPath $InstallDir)) { Write-Host 'Agent is not installed; nothing to remove.'; return }
if ($NoService -and $Action -ne 'install') { throw '-NoService is only valid for installation' }
if ($Action -eq 'start' -and $Hub) { throw 'Use the installed management command for start, without -Hub' }
function Confirm-CodePierRemoval {
  if ($Action -eq 'uninstall') {
    for ($i=0; $i -lt 60; $i++) {
      if (-not (Test-Path -LiteralPath $InstallDir)) { Write-Host 'Agent uninstalled. Panel records and project files were preserved.'; return }
      Start-Sleep -Milliseconds 500
    }
    throw 'Agent directory still exists; Windows cleanup did not complete. Inspect file locks before retrying.'
  }
}
if ($Action -ne 'install' -and -not $Hub) {
  $Helper = Join-Path (Split-Path $PSScriptRoot -Parent) 'scripts/install_agent.py'
  if (-not (Test-Path -LiteralPath $Helper)) { throw 'Use the installed agentctl.ps1 or the complete source package.' }
  $Python = Join-Path $InstallDir 'runtime/.venv/Scripts/python.exe'
  if (-not (Test-Path -LiteralPath $Python)) { $Python = (Get-Command python -ErrorAction Stop).Source }
  $Python = (& $Python -c "import sys; print(getattr(sys, '_base_executable', None) or sys.executable)" | Select-Object -Last 1)
  if ($LASTEXITCODE -ne 0 -or -not $Python) { throw 'No external Python interpreter found' }
  $CodePierFlag = if ($Action -eq 'start') { '--start-service' } else { '--'+$Action }
  $CodePierArgs = @($Helper, $CodePierFlag, '--install-dir', $InstallDir)
  if ($Yes) { $CodePierArgs += '--yes' }
  if ($ExpectedDevice) { $CodePierArgs += @('--expected-device', $ExpectedDevice) }
  & $Python @CodePierArgs
  if ($LASTEXITCODE -ne 0) { throw 'Agent operation failed; see the error above.' }
  Confirm-CodePierRemoval
  return
}
if ($Hub -notmatch '^https?://' -or $Sha256 -cnotmatch '^[a-f0-9]{64}$') { throw 'Invalid panel URL or package checksum' }
if ($Action -eq 'install') {
  if ($Token -notlike 'rdi_*') { throw 'Invalid install ticket' }
  if (-not [IO.Path]::IsPathRooted($AllowRoot) -or -not (Test-Path -LiteralPath $AllowRoot -PathType Container)) { throw 'Allowed directory must already exist and be absolute' }
}
# Existing installations are validated by Python and enter the atomic repair path.
$CodePierTemp = Join-Path ([IO.Path]::GetTempPath()) ('codepier-agent-' + [guid]::NewGuid().ToString('N'))
$OldPythonDir = $env:UV_PYTHON_INSTALL_DIR
$OldUnmanaged = $env:UV_UNMANAGED_INSTALL
$OldNoPath = $env:UV_NO_MODIFY_PATH
try {
  New-Item -ItemType Directory -Path $CodePierTemp -Force | Out-Null
  $CodePierTools = Join-Path $InstallDir 'tools'
  New-Item -ItemType Directory -Path $CodePierTools -Force | Out-Null
  $env:UV_PYTHON_INSTALL_DIR = Join-Path $InstallDir 'python'
  $env:UV_NO_MODIFY_PATH = '1'
  $CodePierUv = Join-Path $CodePierTools 'uv.exe'
  if (-not (Test-Path -LiteralPath $CodePierUv)) {
    $ExistingUv = Get-Command uv -ErrorAction SilentlyContinue
    if ($ExistingUv) { $CodePierUv = $ExistingUv.Source }
    else {
      Write-Host 'Preparing private Python installer...'
      $UvScript = Join-Path $CodePierTemp 'uv.ps1'
      Invoke-WebRequest -UseBasicParsing -TimeoutSec 120 -Uri 'https://astral.sh/uv/0.10.8/install.ps1' -OutFile $UvScript
      if ((Get-FileHash -LiteralPath $UvScript -Algorithm SHA256).Hash.ToLowerInvariant() -cne '800560daa61893c1f00bdfdd4484f2d2db005965bfd7fbeafa2fad3a47aa9b9c') { throw 'Python installer checksum mismatch' }
      $env:UV_UNMANAGED_INSTALL = $CodePierTools
      & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $UvScript
      if ($LASTEXITCODE -ne 0) { throw 'Python installer download failed' }
    }
  }
  & $CodePierUv python install --no-bin 3.13
  if ($LASTEXITCODE -ne 0) { throw 'Python preparation failed' }
  $CodePierPython = (& $CodePierUv python find --managed-python 3.13 | Select-Object -Last 1)
  if ($LASTEXITCODE -ne 0 -or -not $CodePierPython) { throw 'Python not available' }
  $Archive = Join-Path $CodePierTemp 'agent.zip'
  # Python is already prepared above. Its streaming downloader avoids Windows
  # PowerShell's single large Invoke-WebRequest and retries only transient failures.
  $CodePierDownload = @'
import hashlib
import http.client
import os
from pathlib import Path
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_BYTES = 8 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def download(hub, sha256, destination, timeout=30, attempts=4):
    parsed = urllib.parse.urlsplit(hub)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or not re.fullmatch(r"[a-f0-9]{64}", sha256)):
        raise ValueError("Invalid panel URL or package checksum")
    if not 1 <= attempts <= 5 or not 0 < timeout <= 120:
        raise ValueError("Invalid download limits")
    output = Path(destination)
    partial = output.with_name(output.name + ".part")
    if output.exists() or output.is_symlink() or partial.exists() or partial.is_symlink():
        raise ValueError("Download destination already exists")
    url = hub.rstrip("/") + "/agent/agent.zip?sha256=" + sha256
    opener = urllib.request.build_opener(NoRedirect)
    for attempt in range(1, attempts + 1):
        print(f"Downloading Agent package from panel {parsed.hostname} (attempt {attempt}/{attempts})...", flush=True)
        started = time.monotonic()
        size = 0
        checksum = hashlib.sha256()
        try:
            # No enrollment ticket is sent. Never append an unverified partial
            # response or switch to another host/package after a failure.
            request = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
            with opener.open(request, timeout=timeout) as response:
                if response.status != 200:
                    raise ValueError("Unexpected package response status")
                etag = response.headers.get("ETag")
                if etag is not None and etag != '"' + sha256 + '"':
                    raise ValueError("Package identity changed; generate a new command in the panel")
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdigit() or not 0 < int(length) <= MAX_BYTES):
                    raise ValueError("Invalid package response size")
                with partial.open("xb") as stream:
                    while True:
                        if time.monotonic() - started > 120:
                            raise TimeoutError("Package attempt exceeded its time budget")
                        chunk = response.read1(64 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ValueError("Package exceeds size limit")
                        stream.write(chunk)
                        checksum.update(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                if length is not None and size != int(length):
                    raise http.client.IncompleteRead(b"", int(length) - size)
            if checksum.hexdigest() != sha256:
                raise ValueError("Agent package checksum mismatch; generate a new command in the panel")
            os.replace(partial, output)
            print(f"Agent package verified: {size} bytes, SHA-256 matched.", flush=True)
            return
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
            status = exc.code if isinstance(exc, urllib.error.HTTPError) else None
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise ValueError("Panel TLS certificate verification failed") from None
            if status is not None and status not in {408, 429, 500, 502, 503, 504}:
                raise ValueError(f"Panel package request failed (HTTP {status}); generate a new command if it changed") from None
            if attempt == attempts:
                raise ValueError(f"Panel package download failed after {attempts} attempts ({type(exc).__name__}); check this computer's connection to the panel") from None
            delay = min(4, 2 ** (attempt - 1))
            print(f"Package transfer interrupted after {size} bytes; retrying from the beginning in {delay}s.", flush=True)
        finally:
            partial.unlink(missing_ok=True)
        time.sleep(delay)


if __name__ == "__main__":
    try:
        download(sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5]))
    except (ValueError, OSError) as exc:
        print("Agent package download failed: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
'@
  $DownloadHelper = Join-Path $CodePierTemp 'download_agent.py'
  Set-Content -LiteralPath $DownloadHelper -Value $CodePierDownload -Encoding UTF8
  & $CodePierPython $DownloadHelper $Hub $Sha256 $Archive $DownloadTimeoutSec $DownloadAttempts
  if ($LASTEXITCODE -ne 0) { throw 'Panel Agent package download did not complete; see the download stage above.' }
  if ((Get-Item -LiteralPath $Archive).Length -gt 8388608 -or (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant() -cne $Sha256) { throw 'Agent package checksum mismatch; generate a new command in the panel.' }
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  $Zip = [IO.Compression.ZipFile]::OpenRead($Archive)
  try {
    $Entry = $Zip.GetEntry('scripts/install_agent.py')
    if (-not $Entry -or $Entry.Length -gt 131072) { throw 'Invalid installer size' }
    $Helper = Join-Path $CodePierTemp 'install_agent.py'
    [IO.Compression.ZipFileExtensions]::ExtractToFile($Entry, $Helper, $false)
  } finally { $Zip.Dispose() }
  $env:CODEPIER_INSTALL_TOKEN = $Token
  $Token = ''
  $CodePierArgs = @($Helper, '--archive', $Archive, '--sha256', $Sha256, '--hub', $Hub, '--install-dir', $InstallDir, '--uv', $CodePierUv)
  if ($Action -eq 'install') {
    $CodePierArgs += @('--allow', $AllowRoot, '--shell', $Shell)
    if ($NoService) { $CodePierArgs += '--no-service' }
  } else { $CodePierArgs += ('--'+$Action) }
  if ($Yes) { $CodePierArgs += '--yes' }
  if ($ExpectedDevice) { $CodePierArgs += @('--expected-device', $ExpectedDevice) }
  & $CodePierPython @CodePierArgs
  if ($LASTEXITCODE -ne 0) { throw 'Agent operation did not complete; see the error above.' }
  Confirm-CodePierRemoval
} finally {
  Remove-Item Env:CODEPIER_INSTALL_TOKEN -ErrorAction SilentlyContinue
  $env:UV_PYTHON_INSTALL_DIR = $OldPythonDir
  $env:UV_UNMANAGED_INSTALL = $OldUnmanaged
  $env:UV_NO_MODIFY_PATH = $OldNoPath
  Remove-Item -LiteralPath $CodePierTemp -Recurse -Force -ErrorAction SilentlyContinue
}
