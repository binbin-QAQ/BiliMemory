[CmdletBinding()]
param(
  [string]$MaiBotRoot = "",
  [string]$MaiBotPython = "",
  [string]$AMemorixPackageSpec = "MaiBot>=1.3.15,<1.4",
  [switch]$SkipInstall,
  [switch]$DryRun
)

$ErrorActionPreference = "Stop"
function Write-Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message) { Write-Host "    OK  $Message" -ForegroundColor Green }
function Write-Warn([string]$Message) { Write-Host "    WARN $Message" -ForegroundColor Yellow }

$installRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$installer = Join-Path $PSScriptRoot "install_amemorix.py"
$configurator = Join-Path $PSScriptRoot "configure_memory.py"

function Find-MaiBotRoot([string]$Requested) {
  if ($Requested) {
    $resolved = [IO.Path]::GetFullPath($Requested)
    if (Test-Path -LiteralPath (Join-Path $resolved "config")) { return $resolved }
    if (Test-Path -LiteralPath (Join-Path $resolved "modules\MaiBot")) { return (Join-Path $resolved "modules\MaiBot") }
  }
  $base = Join-Path $env:APPDATA "MaiBotOneKeyDesktop"
  if (-not (Test-Path -LiteralPath $base)) { return $null }
  $candidate = Get-ChildItem -LiteralPath $base -Directory -ErrorAction SilentlyContinue |
    Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "modules\MaiBot\config") } |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
  if ($candidate) { return (Join-Path $candidate.FullName "modules\MaiBot") }
  return $null
}

function Find-MaiBotPython([string]$Root) {
  if ($MaiBotPython) {
    $resolved = [IO.Path]::GetFullPath($MaiBotPython)
    if (Test-Path -LiteralPath $resolved) { return $resolved }
  }
  if (-not $Root) { return $null }
  $deployment = Join-Path $Root ".onekey-deployment.json"
  if (Test-Path -LiteralPath $deployment) {
    try {
      $generation = (Get-Content -LiteralPath $deployment -Raw -Encoding UTF8 | ConvertFrom-Json).generation
      $candidate = Join-Path $Root ".onekey\environments\$generation\python.exe"
      if (Test-Path -LiteralPath $candidate) { return $candidate }
    } catch {}
  }
  return Get-ChildItem -LiteralPath (Join-Path $Root ".onekey\environments") -Directory -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    ForEach-Object { Join-Path $_.FullName "python.exe" } |
    Where-Object { Test-Path -LiteralPath $_ } |
    Select-Object -First 1
}

Write-Step "Locating MaiBot and A-Memorix"
$resolvedRoot = Find-MaiBotRoot $MaiBotRoot
$resolvedPython = Find-MaiBotPython $resolvedRoot
if (-not $resolvedRoot) { throw "MaiBot OneKey was not found. Install or start MaiBot OneKey once, or pass -MaiBotRoot." }
if (-not $resolvedPython) { throw "MaiBot Python environment was not found. Pass -MaiBotPython explicitly." }
Write-Ok "MaiBot root: $resolvedRoot"
Write-Ok "MaiBot Python: $resolvedPython"

$configArgs = @(
  $configurator,
  "--mai-bot-root", $resolvedRoot,
  "--embedding-url", "http://127.0.0.1:11435/v1",
  "--model-name", "local-bge-small-zh",
  "--model-id", "bge-small-zh-v1.5",
  "--dimension", "512"
)
$checkOutput = & $resolvedPython @configArgs 2>&1
if ($LASTEXITCODE -ne 0) { throw "A-Memorix check failed: $($checkOutput -join ' ')" }
$check = ($checkOutput -join "`n") | ConvertFrom-Json

if (-not [bool]$check.aMemorixInstalled) {
  Write-Warn "A-Memorix is not installed in this MaiBot environment."
  if ($SkipInstall) {
    Write-Warn "Installation skipped by -SkipInstall."
    exit 1
  }
  $installArgs = @($installer, "--package-spec", $AMemorixPackageSpec)
  if (-not $DryRun) { $installArgs += "--apply" }
  $installOutput = & $resolvedPython @installArgs 2>&1
  $installExit = $LASTEXITCODE
  $installResult = ($installOutput -join "`n") | ConvertFrom-Json
  if ($installResult.output) { Write-Host ($installResult.output.TrimEnd()) }
  if ($DryRun) {
    Write-Ok "Dry run: would install $AMemorixPackageSpec."
    exit 0
  }
  if ($installExit -ne 0 -or -not [bool]$installResult.aMemorixInstalled) {
    throw "A-Memorix installation failed: $($installResult.error)"
  }
  Write-Ok "MaiBot $($installResult.packageVersion) installed; A-Memorix detected."
} else {
  Write-Ok "A-Memorix is already installed."
}

if ($DryRun) {
  Write-Ok "Memory configuration check: $(($checkOutput -join ' ').Trim())"
  Write-Host "Dry run completed. MaiBot configuration was not changed." -ForegroundColor Yellow
  exit 0
}

$configOutput = & $resolvedPython @configArgs --apply 2>&1
if ($LASTEXITCODE -ne 0) { throw "A-Memorix configuration failed: $($configOutput -join ' ')" }
$result = ($configOutput -join "`n") | ConvertFrom-Json
if (-not [bool]$result.applied -or -not [bool]$result.aMemorixInstalled) {
  throw "A-Memorix is installed but configuration did not complete."
}
Write-Ok "A-Memorix enabled with local-bge-small-zh (512 dimensions)."
Write-Host "Embedding URL: http://127.0.0.1:11435/v1"
Write-Host "Data directory: data\a-memorix"
Write-Host "Backups: $($result.backups -join ', ')"
Write-Host "`nA-Memorix setup completed." -ForegroundColor Green