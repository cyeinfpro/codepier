param(
  [ValidateSet('install','upgrade','uninstall','status','start','archive')][string]$Action = 'install',
  [string]$Hub,
  [string]$Token,
  [string]$Sha256,
  [string]$AllowRoot,
  [ValidateSet('full','disabled')][string]$Shell = 'full',
  [string]$InstallDir = (Join-Path $env:USERPROFILE '.codepier-agent'),
  [switch]$NoService,
  [switch]$Yes,
  [switch]$Preview,
  [string]$ExpectedDevice,
  [ValidateRange(5,120)][int]$DownloadTimeoutSec = 30,
  [ValidateRange(1,5)][int]$DownloadAttempts = 4
)
$ErrorActionPreference = 'Stop'
if (-not $PSBoundParameters.ContainsKey('InstallDir') -and $Action -ne 'uninstall') {
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
if ($InstallDir -notmatch '^(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+)') { throw 'Install directory must be absolute, not drive-relative' }
$InstallDir = [IO.Path]::GetFullPath($InstallDir).TrimEnd([IO.Path]::DirectorySeparatorChar)
$HomeDir = [IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd([IO.Path]::DirectorySeparatorChar)
if ($InstallDir -eq $HomeDir -or $HomeDir.StartsWith($InstallDir + '\', [StringComparison]::OrdinalIgnoreCase) -or
    $InstallDir -eq [IO.Path]::GetPathRoot($InstallDir).TrimEnd([IO.Path]::DirectorySeparatorChar)) { throw 'Refusing a home directory, ancestor or filesystem root as Agent installation.' }
if ($Preview -and $Action -ne 'uninstall') { throw '-Preview is valid only for uninstall.' }
if ($Yes -and $Action -ne 'uninstall') { throw '-Yes is valid only for uninstall; archive requires local confirmation.' }
if ($Action -ne 'uninstall' -and $ExpectedDevice -and $ExpectedDevice -cnotmatch '^[A-Za-z0-9_-]{1,100}$') { throw 'Invalid expected panel device identity' }
if ($Action -eq 'archive' -and -not (Test-Path -LiteralPath $InstallDir)) { throw 'No Agent installation to archive.' }
if ($NoService -and $Action -ne 'install') { throw '-NoService is only valid for installation' }
if ($Action -eq 'start' -and $Hub) { throw 'Use the installed management command for start, without -Hub' }
$ReuseExistingPairing = $false
if (($ExpectedDevice -or $Hub) -and $Action -in @('install','upgrade','start')) {
  $ExistingConfig = Join-Path $InstallDir 'config.json'
  if (Test-Path -LiteralPath $ExistingConfig -PathType Leaf) {
    $ExistingConfigItem = Get-Item -LiteralPath $ExistingConfig -Force
    if ($ExistingConfigItem.Length -gt 1048576 -or ($ExistingConfigItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
      throw 'Existing Agent configuration is too large or is a link; no installation started.'
    }
    try { $ExistingConfigValue = Get-Content -LiteralPath $ExistingConfig -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { throw 'Existing Agent config cannot be read; do not delete it.' }
    $ExistingIdentity = $ExistingConfigValue.device_id
    if (-not ($ExistingIdentity -is [string]) -or $ExistingIdentity -cnotmatch '^[A-Za-z0-9_-]{1,100}$' -or
        -not ($ExistingConfigValue.hub_url -is [string]) -or -not $ExistingConfigValue.hub_url) {
      throw 'Existing Agent config is invalid; no installation started. Do not delete config.json.'
    }
    if ($ExpectedDevice -and $ExistingIdentity -cne $ExpectedDevice) {
      throw ('This computer already records another Agent node. No installation started and the ticket was not used. Existing local device ID: ' +
        $ExistingIdentity + '. To keep this Agent, select its original node and copy that node''s install or upgrade command. ' +
        'To intentionally change nodes, generate a Windows archive recovery command, confirm the local old device ID, and then reinstall. Do not delete config.json. ' +
        'Or run the local Windows uninstall command to remove verified Agent installations before a fresh install.')
    }
    if ($Hub -and $ExistingConfigValue.hub_url.TrimEnd('/') -cne $Hub.TrimEnd('/')) {
      throw 'This command belongs to a different panel (Hub). No installation started and the ticket was not used. Select the original node on its original panel to upgrade. To intentionally move to another panel, archive the old installation with local confirmation first. Or run the local Windows uninstall command to remove verified Agent installations before a fresh install.'
    }
    $ReuseExistingPairing = ($Action -eq 'install' -and $ExpectedDevice -and $Hub -and
      ($ExistingConfigValue.secret -is [string]) -and $ExistingConfigValue.secret.Length -gt 0)
  }
}
function Get-CodePierSHA256 {
  param([Parameter(Mandatory=$true)][string]$Path)
  # PowerShell.Utility may be unavailable in noninteractive Windows PowerShell
  # 5.1 installations; use the built-in .NET SHA256 implementation instead.
  $Stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
  try {
    $Hasher = [Security.Cryptography.SHA256]::Create()
    try {
      return [BitConverter]::ToString($Hasher.ComputeHash($Stream)).Replace('-', '').ToLowerInvariant()
    } finally { $Hasher.Dispose() }
  } finally { $Stream.Dispose() }
}
function Complete-CodePierArchive {
  param([string]$PlanPath, [string]$Source)
  $PlanItem = Get-Item -LiteralPath $PlanPath -Force -ErrorAction Stop
  if ($PlanItem.PSIsContainer -or $PlanItem.Length -gt 16384 -or ($PlanItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Invalid archive handoff file.' }
  $Plan = Get-Content -LiteralPath $PlanPath -Encoding UTF8 -Raw | ConvertFrom-Json
  $SourcePath = [IO.Path]::GetFullPath($Source).TrimEnd([IO.Path]::DirectorySeparatorChar)
  if ($Plan.schema -ne 1 -or [string]$Plan.lock_token -cnotmatch '^[a-f0-9]{32}$' -or -not $Plan.backup) { throw 'Invalid archive handoff schema.' }
  $PlanSource = [IO.Path]::GetFullPath([string]$Plan.source).TrimEnd([IO.Path]::DirectorySeparatorChar)
  $Target = [IO.Path]::GetFullPath([string]$Plan.backup).TrimEnd([IO.Path]::DirectorySeparatorChar)
  # Use pure .NET path operations instead of Split-Path -Path: in Windows
  # PowerShell 5.1 its provider may resolve non-existent archive destinations
  # as wildcard paths and produce a different/missing leaf.
  $SourceParent = [IO.Path]::GetDirectoryName($SourcePath)
  $TargetParent = [IO.Path]::GetDirectoryName($Target)
  $SourceLeaf = [IO.Path]::GetFileName($SourcePath)
  $TargetLeaf = [IO.Path]::GetFileName($Target)
  $Prefix = '^' + [regex]::Escape($SourceLeaf + '.recovery-') + '[0-9]{8}-[0-9]{6}-[a-f0-9]{8}$'
  if (-not [string]::Equals($SourcePath, $PlanSource, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Archive handoff source verification failed.'
  }
  if (-not [string]::Equals($SourceParent, $TargetParent, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Archive handoff destination parent verification failed.'
  }
  if ($TargetLeaf -cnotmatch $Prefix) { throw 'Archive handoff backup name verification failed.' }
  $OwnedRoot = $SourcePath
  $LockVerified = $false
  try {
    foreach ($Path in @($SourcePath, (Join-Path $SourcePath '.install.lock'), (Join-Path $SourcePath '.install.lock/archive.json'))) {
      $Item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
      if ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Archive source or maintenance lock became a link.' }
    }
    $Owner = Get-Content -LiteralPath (Join-Path $SourcePath '.install.lock/archive.json') -Encoding UTF8 -Raw | ConvertFrom-Json
    if ([string]$Owner.lock_token -cne [string]$Plan.lock_token) { throw 'Archive maintenance lock ownership changed.' }
    $LockVerified = $true
    foreach ($Name in @('config.json', 'management.json', 'service.xml')) {
      if (-not $Plan.files -or -not ($Plan.files.PSObject.Properties.Name -contains $Name)) { throw 'Incomplete archive file snapshot.' }
      $ExpectedHash = $Plan.files.$Name
      $File = Join-Path $SourcePath $Name
      $Item = Get-Item -LiteralPath $File -Force -ErrorAction SilentlyContinue
      if ($null -eq $ExpectedHash) {
        if ($null -ne $Item) { throw 'Agent files changed after confirmation; archive refused.' }
      } else {
        if ([string]$ExpectedHash -cnotmatch '^[a-f0-9]{64}$' -or $null -eq $Item -or $Item.PSIsContainer -or
            ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
            (Get-CodePierSHA256 -Path $File) -cne [string]$ExpectedHash) { throw 'Agent files changed after confirmation; archive refused.' }
      }
    }
    # Directory.Move never merges into a target created concurrently, unlike Move-Item.
    Set-Location -LiteralPath ([IO.Path]::GetTempPath())
    [IO.Directory]::Move($SourcePath, $Target)
    $OwnedRoot = $Target
    if ((Test-Path -LiteralPath $SourcePath) -or -not (Test-Path -LiteralPath $Target -PathType Container)) { throw 'Archive rename was not confirmed.' }
    Write-Host ('Old Agent installation was preserved at: ' + $Target)
    Write-Host 'Generate a fresh Windows install command for the intended panel node.'
  } finally {
    if ($LockVerified) {
      $Lock = Join-Path $OwnedRoot '.install.lock'
      $OwnerFile = Join-Path $Lock 'archive.json'
      $Owner = Get-Content -LiteralPath $OwnerFile -Encoding UTF8 -Raw | ConvertFrom-Json
      if ([string]$Owner.lock_token -ceq [string]$Plan.lock_token -and
          -not ((Get-Item -LiteralPath $Lock -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        Remove-Item -LiteralPath $OwnerFile -ErrorAction Stop
        [IO.Directory]::Delete($Lock, $false)
      }
    }
  }
}
function Assert-CodePierPlainPath {
  param([string]$Path)
  if (-not $Path -or $Path -notmatch '^(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+)' -or $Path -match '[\x00-\x1f\x7f]') { throw 'Invalid cleanup path.' }
  $Full = [IO.Path]::GetFullPath($Path).TrimEnd([IO.Path]::DirectorySeparatorChar)
  $Cursor = $Full
  while ($Cursor) {
    $Item = Get-Item -LiteralPath $Cursor -Force -ErrorAction Stop
    if ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Cleanup path contains a reparse point.' }
    $Parent = [IO.Directory]::GetParent($Cursor)
    if ($null -eq $Parent) { break }
    $Cursor = $Parent.FullName
  }
  return $Full
}
function Assert-CodePierUninstallTree {
  param([string]$Path)
  $Pending = New-Object System.Collections.Stack
  $Pending.Push($Path)
  $Count = 0
  while ($Pending.Count -gt 0) {
    $Item = Get-Item -LiteralPath ([string]$Pending.Pop()) -Force -ErrorAction Stop
    $Count++
    if ($Count -gt 200000) { throw 'Cleanup tree exceeds the inspection limit.' }
    if ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Cleanup tree contains a link; manual review is required.' }
    if ($Item.PSIsContainer) {
      foreach ($Child in @(Get-ChildItem -LiteralPath $Item.FullName -Force -ErrorAction Stop)) { $Pending.Push($Child.FullName) }
    }
  }
}
function Assert-CodePierUninstallRoot {
  param($Root, [switch]$SkipEntries)
  if (-not ($Root.source -is [string]) -or [string]$Root.lock_token -cnotmatch '^[a-f0-9]{32}$') { throw 'Invalid cleanup root or lock token.' }
  $Source = Assert-CodePierPlainPath -Path $Root.source
  $HomePath = [IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd([IO.Path]::DirectorySeparatorChar)
  if ($Source -eq $HomePath -or $HomePath.StartsWith($Source + '\', [StringComparison]::OrdinalIgnoreCase) -or
      $Source -eq [IO.Path]::GetPathRoot($Source).TrimEnd([IO.Path]::DirectorySeparatorChar)) { throw 'Refusing to clean a home directory, ancestor or filesystem root.' }
  $SourceItem = Get-Item -LiteralPath $Source -Force -ErrorAction Stop
  if (-not $SourceItem.PSIsContainer) { throw 'Cleanup source is not a directory.' }
  $Lock = Join-Path $Source '.install.lock'
  $OwnerPath = Join-Path $Lock 'uninstall.json'
  $null = Assert-CodePierPlainPath -Path $OwnerPath
  $OwnerItem = Get-Item -LiteralPath $OwnerPath -Force -ErrorAction Stop
  if ($OwnerItem.PSIsContainer -or $OwnerItem.Length -gt 16384) { throw 'Invalid cleanup lock record.' }
  $Owner = Get-Content -LiteralPath $OwnerPath -Encoding UTF8 -Raw | ConvertFrom-Json
  if ([string]$Owner.lock_token -cne [string]$Root.lock_token) { throw 'Cleanup maintenance lock ownership changed.' }
  if (@(Get-ChildItem -LiteralPath $Lock -Force -ErrorAction Stop).Count -ne 1) { throw 'Unexpected maintenance lock contents.' }
  if ($SkipEntries) { return $Source }
  if (-not ($Root.entries -is [Array]) -or -not $Root.files) { throw 'Incomplete cleanup snapshot.' }
  $Names = @()
  foreach ($Name in @($Root.entries)) {
    if (-not ($Name -is [string]) -or -not $Name -or $Name -in @('.', '..', '.install.lock') -or
        $Name -match '[\\/:*?"<>|\x00-\x1f\x7f]' -or $Name.EndsWith('.') -or $Name.EndsWith(' ') -or $Names -contains $Name) {
      throw 'Invalid or duplicate cleanup entry name.'
    }
    $Names += $Name
  }
  $Actual = @(Get-ChildItem -LiteralPath $Source -Force -ErrorAction Stop | ForEach-Object { $_.Name })
  if ($Actual.Count -ne $Names.Count + 1 -or @($Actual | Where-Object { $_ -cne '.install.lock' -and $Names -cnotcontains $_ }).Count -ne 0) { throw 'Agent directory entries changed after confirmation.' }
  foreach ($Name in @('config.json', 'management.json', 'service.xml')) {
    if (-not ($Root.files.PSObject.Properties.Name -ccontains $Name)) { throw 'Incomplete cleanup file fingerprint.' }
    $Expected = $Root.files.$Name
    $File = Join-Path $Source $Name
    if ($null -eq $Expected) {
      if (Test-Path -LiteralPath $File) { throw 'Agent files changed after confirmation.' }
    } else {
      if (-not ($Expected -is [string]) -or $Expected -cnotmatch '^[a-f0-9]{64}$') { throw 'Invalid cleanup fingerprint.' }
      $Item = Get-Item -LiteralPath $File -Force -ErrorAction Stop
      if ($Item.PSIsContainer -or ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
          (Get-CodePierSHA256 -Path $File) -cne $Expected) { throw 'Agent files changed after confirmation.' }
    }
  }
  $MarkerPath = Join-Path $Source '.uninstall.json'
  $MarkerItem = Get-Item -LiteralPath $MarkerPath -Force -ErrorAction Stop
  if ($Names -cnotcontains '.uninstall.json' -or $MarkerItem.PSIsContainer -or $MarkerItem.Length -gt 16384 -or
      ($MarkerItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Missing or unsafe cleanup recovery marker.' }
  $Marker = Get-Content -LiteralPath $MarkerPath -Encoding UTF8 -Raw | ConvertFrom-Json
  $Sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  if ($Marker.schema -ne 1 -or $Marker.kind -cne 'codepier-local-uninstall') { throw 'Cleanup recovery marker schema changed.' }
  if ($Marker.stage -cnotin @('files_pending', 'files_failed')) { throw 'Cleanup recovery marker stage changed.' }
  # Windows PowerShell/.NET can expand an existing 8.3 short path while
  # Python preserves its spelling. Normalize both verified absolute paths.
  $MarkerSource = Assert-CodePierPlainPath -Path ([string]$Marker.source)
  if (-not [string]::Equals($MarkerSource, $Source, [StringComparison]::OrdinalIgnoreCase)) { throw 'Cleanup recovery marker source changed.' }
  if ([string]$Marker.owner_sid -cne $Sid) { throw 'Cleanup recovery marker account SID changed.' }
  Assert-CodePierUninstallTree -Path $Source
  return $Source
}
function Remove-CodePierUninstallLock {
  param($Root)
  if (-not (Test-Path -LiteralPath $Root.source)) { return }
  $Source = Assert-CodePierUninstallRoot -Root $Root -SkipEntries
  $Lock = Join-Path $Source '.install.lock'
  [IO.File]::Delete((Join-Path $Lock 'uninstall.json'))
  [IO.Directory]::Delete($Lock)
}
function Assert-CodePierUninstallAlias {
  param([string]$Alias, [string[]]$Sources)
  $Expected = Join-Path $env:USERPROFILE '.remote-dev-agent'
  $Target = Join-Path $env:USERPROFILE '.codepier-agent'
  if (-not $Alias -or [IO.Path]::GetFullPath($Alias) -ine [IO.Path]::GetFullPath($Expected) -or
      ($Sources -notcontains $Target -and (Test-Path -LiteralPath $Target))) { throw 'Unrecognized cleanup alias.' }
  $null = Assert-CodePierPlainPath -Path $env:USERPROFILE
  $Item = Get-Item -LiteralPath $Alias -Force -ErrorAction Stop
  if (-not $Item.PSIsContainer -or -not ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
      @($Item.Target).Count -ne 1 -or [string]$Item.Target -ine $Target) { throw 'Cleanup alias target changed.' }
}
function Set-CodePierUninstallFailure {
  param($Root, [string]$Message)
  $Source = Assert-CodePierUninstallRoot -Root $Root -SkipEntries
  $File = Join-Path $Source '.uninstall.json'
  $null = Assert-CodePierPlainPath -Path $File
  $Item = Get-Item -LiteralPath $File -Force -ErrorAction Stop
  if ($Item.PSIsContainer -or $Item.Length -gt 16384) { throw 'Unsafe cleanup failure marker.' }
  $Marker = Get-Content -LiteralPath $File -Encoding UTF8 -Raw | ConvertFrom-Json
  if ($Marker.schema -ne 1 -or $Marker.kind -cne 'codepier-local-uninstall') { throw 'Cleanup failure marker schema changed.' }
  $MarkerSource = Assert-CodePierPlainPath -Path ([string]$Marker.source)
  if (-not [string]::Equals($MarkerSource, $Source, [StringComparison]::OrdinalIgnoreCase)) { throw 'Cleanup failure marker source changed.' }
  if ([string]$Marker.owner_sid -cne [Security.Principal.WindowsIdentity]::GetCurrent().User.Value) { throw 'Cleanup failure marker account SID changed.' }
  $Marker | Add-Member -NotePropertyName stage -NotePropertyValue 'files_failed' -Force
  $SafeMessage = 'Local cleanup failed; inspect remaining paths and retry after closing file locks.'
  $Marker | Add-Member -NotePropertyName last_error -NotePropertyValue $SafeMessage -Force
  $Temporary = Join-Path $Source ('.uninstall-failure-' + $Root.lock_token + '.tmp')
  $Bytes = (New-Object Text.UTF8Encoding($false)).GetBytes(($Marker | ConvertTo-Json -Depth 8))
  $Stream = [IO.File]::Open($Temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
  try {
    try { $Stream.Write($Bytes, 0, $Bytes.Length); $Stream.Flush() } finally { $Stream.Dispose() }
    [IO.File]::Replace($Temporary, $File, $null)
  } finally { if (Test-Path -LiteralPath $Temporary) { [IO.File]::Delete($Temporary) } }
}
function Assert-CodePierUninstallTasksAbsent {
  $Service = New-Object -ComObject Schedule.Service
  $Service.Connect()
  foreach ($Name in @('CodePierAgent', 'RemoteDevAgent')) {
    $Task = $null
    try { $Task = $Service.GetFolder('\').GetTask($Name) } catch {
      $Missing = $false
      $ErrorObject = $_.Exception
      for ($i = 0; $i -lt 8 -and $null -ne $ErrorObject; $i++) {
        if ($ErrorObject.HResult -eq -2147024894) { $Missing = $true; break }
        $ErrorObject = $ErrorObject.InnerException
      }
      if (-not $Missing) { throw 'Cannot verify Windows task removal; cleanup is incomplete.' }
    }
    if ($null -ne $Task) { throw 'An Agent task remains or was recreated; cleanup is incomplete.' }
  }
}
function Complete-CodePierUninstall {
  param([string]$PlanPath)
  $null = Assert-CodePierPlainPath -Path $PlanPath
  $PlanItem = Get-Item -LiteralPath $PlanPath -Force -ErrorAction Stop
  if ($PlanItem.PSIsContainer -or $PlanItem.Length -gt 1048576) { throw 'Invalid cleanup handoff file.' }
  $Plan = Get-Content -LiteralPath $PlanPath -Encoding UTF8 -Raw | ConvertFrom-Json
  if ($Plan.schema -ne 1 -or -not ($Plan.roots -is [Array]) -or -not ($Plan.aliases -is [Array]) -or
      $Plan.roots.Count -gt 16 -or $Plan.aliases.Count -gt 1) { throw 'Invalid cleanup handoff schema.' }
  $Roots = @($Plan.roots)
  $Sources = @()
  $PreviousLocation = (Get-Location).ProviderPath
  $PreviousNative = [Environment]::CurrentDirectory
  $WorkDirectory = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($PlanPath))
  $ExitDirectory = [IO.Directory]::GetParent($WorkDirectory).FullName
  $ChangedLocation = $false
  try {
    # Validate every root and alias before the first deletion.
    foreach ($Root in $Roots) {
      $Source = Assert-CodePierUninstallRoot -Root $Root
      foreach ($Other in $Sources) {
        if ($Other -eq $Source -or $Other.StartsWith($Source + '\', [StringComparison]::OrdinalIgnoreCase) -or
            $Source.StartsWith($Other + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Cleanup roots overlap.' }
      }
      $Sources += $Source
    }
    foreach ($Alias in @($Plan.aliases)) { Assert-CodePierUninstallAlias -Alias $Alias -Sources $Sources }
    foreach ($Source in $Sources) {
      if ($WorkDirectory -eq $Source -or $WorkDirectory.StartsWith($Source + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Cleanup handoff must be outside every source directory.' }
    }
    $null = Assert-CodePierPlainPath -Path $WorkDirectory
    # PowerShell's provider location and the process working directory are distinct.
    # Release both before removing an installation containing the invoking shell.
    Set-Location -LiteralPath $WorkDirectory
    [Environment]::CurrentDirectory = $WorkDirectory
    $ChangedLocation = $true
    Assert-CodePierUninstallTasksAbsent
    foreach ($Root in $Roots) {
      Assert-CodePierUninstallTasksAbsent
      $Source = Assert-CodePierUninstallRoot -Root $Root
      $Created = (Get-Item -LiteralPath $Source -Force).CreationTimeUtc.Ticks
      $Evidence = @{}
      foreach ($Name in @('config.json', 'management.json', 'service.xml', '.uninstall.json')) {
        $File = Join-Path $Source $Name
        if (Test-Path -LiteralPath $File) { $Evidence[$Name] = [IO.File]::ReadAllBytes($File) }
      }
      try {
        foreach ($Name in @($Root.entries)) {
          if ($Name -in @('config.json', 'management.json', 'service.xml', '.uninstall.json')) { continue }
          $null = Assert-CodePierUninstallRoot -Root $Root -SkipEntries
          $Item = Join-Path $Source $Name
          Assert-CodePierUninstallTree -Path $Item
          Remove-Item -LiteralPath $Item -Recurse -Force -ErrorAction Stop
          if (Test-Path -LiteralPath $Item) { throw 'An Agent cleanup entry remains.' }
        }
        # Unknown/new entries must survive; never recursively remove the source.
        $Remaining = @(Get-ChildItem -LiteralPath $Source -Force -ErrorAction Stop | ForEach-Object { $_.Name })
        if (@($Remaining | Where-Object { $_ -notin @('config.json', 'management.json', 'service.xml', '.uninstall.json', '.install.lock') }).Count) { throw 'New directory content appeared during cleanup.' }
        $null = Assert-CodePierUninstallRoot -Root $Root -SkipEntries
        foreach ($Name in @('config.json', 'management.json', 'service.xml', '.uninstall.json')) {
          $File = Join-Path $Source $Name
          if ($Evidence.ContainsKey($Name)) {
            if ((Get-CodePierSHA256 -Path $File) -cne ([BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($Evidence[$Name])).Replace('-', '').ToLowerInvariant())) { throw 'Cleanup evidence changed during removal.' }
            [IO.File]::Delete($File)
          }
        }
        Remove-CodePierUninstallLock -Root $Root
        [IO.Directory]::Delete($Source)
      } catch {
        # Preserve resumability even if removing the final empty directory fails.
        # Restore only missing evidence in the same directory; never overwrite edits.
        if (Test-Path -LiteralPath $Source) {
          $null = Assert-CodePierPlainPath -Path $Source
          $Restore = (Get-Item -LiteralPath $Source -Force).CreationTimeUtc.Ticks -eq $Created
          $LockPath = Join-Path $Source '.install.lock'
          if (Test-Path -LiteralPath $LockPath) {
            try { $null = Assert-CodePierUninstallRoot -Root $Root -SkipEntries } catch { $Restore = $false }
          }
          foreach ($Item in @(Get-ChildItem -LiteralPath $Source -Force -ErrorAction Stop)) {
            if ($Item.Name -cne '.install.lock' -and -not $Evidence.ContainsKey($Item.Name)) { $Restore = $false }
          }
          if ($Restore) {
            foreach ($Name in @('.uninstall.json', 'config.json', 'management.json', 'service.xml')) {
              $File = Join-Path $Source $Name
              if ($Evidence.ContainsKey($Name) -and -not (Test-Path -LiteralPath $File)) {
                $Stream = [IO.File]::Open($File, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
                try { $Bytes = $Evidence[$Name]; $Stream.Write($Bytes, 0, $Bytes.Length); $Stream.Flush() } finally { $Stream.Dispose() }
              }
            }
          }
        }
        throw
      }
    }
    foreach ($Alias in @($Plan.aliases)) {
      Assert-CodePierUninstallAlias -Alias $Alias -Sources $Sources
      [IO.Directory]::Delete($Alias)
    }
    foreach ($Source in $Sources) { if (Test-Path -LiteralPath $Source) { throw 'An Agent directory remains after cleanup.' } }
    Assert-CodePierUninstallTasksAbsent
    Write-Host 'Local CodePier cleanup completed.'
  } catch {
    $Failure = $_
    foreach ($Root in $Roots) {
      if (Test-Path -LiteralPath (Join-Path $Root.source '.install.lock')) {
        try { Set-CodePierUninstallFailure -Root $Root -Message $Failure.Exception.Message } catch { Write-Warning 'Cleanup recovery marker could not be safely updated.' }
        try { Remove-CodePierUninstallLock -Root $Root } catch { Write-Warning 'Cleanup lock could not be safely released; inspect it before retrying.' }
      }
    }
    throw ('Local CodePier cleanup did not complete. Remaining evidence was preserved for retry. ' + $Failure.Exception.Message)
  } finally {
    if ($ChangedLocation) {
      # The caller removes its temporary handoff directory after we return.
      # Restore only still-existing external locations; otherwise use its parent.
      $RestoreLocation = $ExitDirectory
      $RestoreNative = $ExitDirectory
      foreach ($Candidate in @($PreviousLocation, $PreviousNative)) {
        $Safe = $Candidate -and (Test-Path -LiteralPath $Candidate -PathType Container)
        foreach ($Source in $Sources) {
          if ($Candidate -eq $Source -or ($Candidate -and $Candidate.StartsWith($Source + '\', [StringComparison]::OrdinalIgnoreCase))) { $Safe = $false }
        }
        if ($Safe) {
          try { $null = Assert-CodePierPlainPath -Path $Candidate } catch { $Safe = $false }
        }
        if ($Safe -and $Candidate -eq $PreviousLocation) { $RestoreLocation = $Candidate }
        if ($Safe -and $Candidate -eq $PreviousNative) { $RestoreNative = $Candidate }
      }
      Set-Location -LiteralPath $RestoreLocation
      [Environment]::CurrentDirectory = $RestoreNative
    }
  }
}

if ($Action -ne 'install' -and -not $Hub) {
  $Helper = Join-Path (Split-Path $PSScriptRoot -Parent) 'scripts/install_agent.py'
  if (-not (Test-Path -LiteralPath $Helper)) { throw 'Use the installed agentctl.ps1 or the complete source package.' }
  $Python = Join-Path $InstallDir 'runtime/.venv/Scripts/python.exe'
  if (-not (Test-Path -LiteralPath $Python)) { $Python = (Get-Command python -ErrorAction Stop).Source }
  $Python = (& $Python -c "import sys; print(getattr(sys, '_base_executable', None) or sys.executable)" | Select-Object -Last 1)
  if ($LASTEXITCODE -ne 0 -or -not $Python) { throw 'No external Python interpreter found' }
  $CodePierFlag = if ($Action -eq 'start') { '--start-service' } elseif ($Action -eq 'archive') { '--archive-install' } else { '--'+$Action }
  $CodePierArgs = @($Helper, $CodePierFlag, '--install-dir', $InstallDir)
  if ($Yes) { $CodePierArgs += '--yes' }
  if ($Preview) { $CodePierArgs += '--uninstall-preview' }
  if ($ExpectedDevice -and $Action -ne 'uninstall') { $CodePierArgs += @('--expected-device', $ExpectedDevice) }
  if ($Action -eq 'uninstall' -and -not $Preview) {
    $LocalTemp = Join-Path ([IO.Path]::GetTempPath()) ('codepier-uninstall-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $LocalTemp | Out-Null
    try {
      $UninstallHandoff = Join-Path $LocalTemp 'uninstall_handoff.json'
      $CodePierArgs += @('--uninstall-handoff-file', $UninstallHandoff)
      & $Python @CodePierArgs
      if ($LASTEXITCODE -ne 0) { throw 'Agent cleanup validation failed; see the error above.' }
      Complete-CodePierUninstall -PlanPath $UninstallHandoff
    } finally { Remove-Item -LiteralPath $LocalTemp -Recurse -Force -ErrorAction SilentlyContinue }
  } elseif ($Action -eq 'archive') {
    $LocalTemp = Join-Path ([IO.Path]::GetTempPath()) ('codepier-archive-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $LocalTemp | Out-Null
    try {
      $ArchiveHandoff = Join-Path $LocalTemp 'archive_handoff.json'
      $CodePierArgs += @('--archive-handoff-file', $ArchiveHandoff)
      & $Python @CodePierArgs
      if ($LASTEXITCODE -ne 0) { throw 'Agent archive validation failed; see the error above.' }
      Complete-CodePierArchive -PlanPath $ArchiveHandoff -Source $InstallDir
    } finally { Remove-Item -LiteralPath $LocalTemp -Recurse -Force -ErrorAction SilentlyContinue }
  } else {
    & $Python @CodePierArgs
    if ($LASTEXITCODE -ne 0) { throw 'Agent operation failed; see the error above.' }
  }
  return
}
if ($Hub -notmatch '^https?://' -or $Sha256 -cnotmatch '^[a-f0-9]{64}$') { throw 'Invalid panel URL or package checksum' }
if ($Action -eq 'install' -and -not $ReuseExistingPairing) {
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
  $ToolBase = if ($Action -eq 'uninstall') { $CodePierTemp } else { $InstallDir }
  $CodePierTools = Join-Path $ToolBase 'tools'
  $CodePierPython = $null
  if ($Action -eq 'uninstall') {
    # Reuse an existing interpreter; never prepare tools inside a cleanup root.
    $ExistingPython = Join-Path $InstallDir 'runtime/.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $ExistingPython -PathType Leaf)) {
      $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
      $ExistingPython = if ($PythonCommand) { $PythonCommand.Source } else { $null }
    }
    if ($ExistingPython) {
      $CandidatePython = (& $ExistingPython -c "import sys; print(getattr(sys, '_base_executable', None) or sys.executable)" | Select-Object -Last 1)
      if ($LASTEXITCODE -eq 0 -and $CandidatePython -and (Test-Path -LiteralPath $CandidatePython -PathType Leaf)) { $CodePierPython = $CandidatePython }
    }
  }
  New-Item -ItemType Directory -Path $CodePierTools -Force | Out-Null
  $env:UV_PYTHON_INSTALL_DIR = Join-Path $ToolBase 'python'
  $env:UV_NO_MODIFY_PATH = '1'
  $CodePierUv = Join-Path $CodePierTools 'uv.exe'
  if (-not $CodePierPython -and -not (Test-Path -LiteralPath $CodePierUv)) {
    $ExistingUv = Get-Command uv -ErrorAction SilentlyContinue
    if ($ExistingUv) { $CodePierUv = $ExistingUv.Source }
    else {
      Write-Host 'Preparing private Python installer...'
      $UvScript = Join-Path $CodePierTemp 'uv.ps1'
      Invoke-WebRequest -UseBasicParsing -TimeoutSec 120 -Uri 'https://astral.sh/uv/0.10.8/install.ps1' -OutFile $UvScript
      if ((Get-CodePierSHA256 -Path $UvScript) -cne '800560daa61893c1f00bdfdd4484f2d2db005965bfd7fbeafa2fad3a47aa9b9c') { throw 'Python installer checksum mismatch' }
      $env:UV_UNMANAGED_INSTALL = $CodePierTools
      & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $UvScript
      if ($LASTEXITCODE -ne 0) { throw 'Python installer download failed' }
    }
  }
  if (-not $CodePierPython) {
    & $CodePierUv python install --no-bin 3.13
    if ($LASTEXITCODE -ne 0) { throw 'Python preparation failed' }
    $CodePierPython = (& $CodePierUv python find --managed-python 3.13 | Select-Object -Last 1)
    if ($LASTEXITCODE -ne 0 -or -not $CodePierPython) { throw 'Python not available' }
  }
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
  if ((Get-Item -LiteralPath $Archive).Length -gt 8388608 -or (Get-CodePierSHA256 -Path $Archive) -cne $Sha256) { throw 'Agent package checksum mismatch; generate a new command in the panel.' }
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
    if (-not $ReuseExistingPairing) { $CodePierArgs += @('--allow', $AllowRoot, '--shell', $Shell) }
    if ($NoService) { $CodePierArgs += '--no-service' }
  } elseif ($Action -eq 'archive') { $CodePierArgs += '--archive-install' } else { $CodePierArgs += ('--'+$Action) }
  if ($Yes) { $CodePierArgs += '--yes' }
  if ($Preview) { $CodePierArgs += '--uninstall-preview' }
  if ($ExpectedDevice -and $Action -ne 'uninstall') { $CodePierArgs += @('--expected-device', $ExpectedDevice) }
  if ($Action -eq 'archive') {
    $ArchiveHandoff = Join-Path $CodePierTemp 'archive_handoff.json'
    $CodePierArgs += @('--archive-handoff-file', $ArchiveHandoff)
  }
  if ($Action -eq 'uninstall' -and -not $Preview) {
    $UninstallHandoff = Join-Path $CodePierTemp 'uninstall_handoff.json'
    $CodePierArgs += @('--uninstall-handoff-file', $UninstallHandoff)
  }
  & $CodePierPython @CodePierArgs
  if ($LASTEXITCODE -ne 0) { throw 'Agent operation did not complete; see the error above.' }
  if ($Action -eq 'archive') { Complete-CodePierArchive -PlanPath $ArchiveHandoff -Source $InstallDir }
  if ($Action -eq 'uninstall' -and -not $Preview) { Complete-CodePierUninstall -PlanPath $UninstallHandoff }
} finally {
  Remove-Item Env:CODEPIER_INSTALL_TOKEN -ErrorAction SilentlyContinue
  $env:UV_PYTHON_INSTALL_DIR = $OldPythonDir
  $env:UV_UNMANAGED_INSTALL = $OldUnmanaged
  $env:UV_NO_MODIFY_PATH = $OldNoPath
  Remove-Item -LiteralPath $CodePierTemp -Recurse -Force -ErrorAction SilentlyContinue
}
