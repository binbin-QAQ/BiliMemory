[CmdletBinding()]
param(
  [string]$InstallRoot = "",
  [string]$MaiBotRoot = "",
  [string]$MaiBotPython = "",
  [string]$MaiBotExe = "",
  [ValidateSet("global", "chat")]
  [string]$MemoryScope = "global",
  [string]$MemoryChatId = "",
  [switch]$SkipMemoryConfiguration,
  [string]$AMemorixPackageSpec = "MaiBot>=1.3.15,<1.4",
  [switch]$SkipAMemorixInstall,
  [switch]$InstallPrerequisites,
  [switch]$SkipPlaywrightBrowser,
  [switch]$NoStart,
  [switch]$DryRun
)

$ErrorActionPreference = "Stop"
function Write-Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message) { Write-Host "    OK  $Message" -ForegroundColor Green }
function Write-Warn([string]$Message) { Write-Host "    WARN $Message" -ForegroundColor Yellow }

if (-not $InstallRoot) { $InstallRoot = $PSScriptRoot }
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$appDir = Join-Path $InstallRoot "bilibili-memory"
$embeddingDir = Join-Path $InstallRoot "local-embedding"
$venvDir = Join-Path $InstallRoot ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $appDir)) { throw "BiliMemory source was not found under $InstallRoot. Extract the repository and run install.cmd there." }
if (-not (Test-Path -LiteralPath $embeddingDir)) { throw "local-embedding directory was not found under $InstallRoot." }
if ($MemoryChatId -and -not $PSBoundParameters.ContainsKey("MemoryScope")) { $MemoryScope = "chat" }
if ($MemoryScope -eq "chat" -and -not $MemoryChatId) { throw "-MemoryScope chat requires -MemoryChatId <A-Memorix chat id>." }

function Refresh-Path {
  $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
  $user = [Environment]::GetEnvironmentVariable("Path", "User")
  $env:Path = "$machine;$user"
}

function Install-WingetPackage([string]$Id, [string]$Label) {
  if (-not $InstallPrerequisites) { throw "$Label is missing. Install it or rerun with -InstallPrerequisites." }
  if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) { throw "$Label is missing and winget is unavailable." }
  Write-Step "Installing $Label with winget"
  & winget.exe install --id $Id --exact --silent --accept-source-agreements --accept-package-agreements --disable-interactivity
  if ($LASTEXITCODE -ne 0) { throw "winget failed to install $Label." }
  Refresh-Path
}

function Resolve-Python312 {
  $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
  if ($pyLauncher) {
    $path = & $pyLauncher.Source -3.12 -c "import sys; print(sys.executable)" 2>$null
    if ($LASTEXITCODE -eq 0 -and $path -and (Test-Path -LiteralPath $path.Trim())) { return $path.Trim() }
  }
  foreach ($candidate in @(
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
    (Join-Path $env:ProgramFiles "Python312\python.exe")
  )) {
    if (Test-Path -LiteralPath $candidate) { return $candidate }
  }
  $python = Get-Command python.exe -ErrorAction SilentlyContinue
  if ($python -and $python.Source -notmatch "\\WindowsApps\\") {
    try {
      $version = & $python.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
      if ($LASTEXITCODE -eq 0 -and $version.Trim() -eq "3.12") { return $python.Source }
    } catch {}
  }
  return $null
}

Write-Step "Checking Windows and source layout"
if ($env:OS -ne "Windows_NT") { throw "This installer supports Windows only." }
Write-Ok "Source: $InstallRoot"

$python = Resolve-Python312
$missingPrerequisites = @()
if (-not $python) {
  if ($DryRun) {
    Write-Warn "Python 3.12 is missing."
    $missingPrerequisites += "Python 3.12"
  } else {
    Install-WingetPackage "Python.Python.3.12" "Python 3.12"
    $python = Resolve-Python312
    if (-not $python) { throw "Python 3.12 was not found after installation." }
  }
}
if ($python) { Write-Ok "Python: $python" }

$node = (Get-Command node.exe -ErrorAction SilentlyContinue).Source
if (-not $node) {
  if ($DryRun) {
    Write-Warn "Node.js is missing."
    $missingPrerequisites += "Node.js"
  } else {
    Install-WingetPackage "OpenJS.NodeJS.LTS" "Node.js LTS"
    $node = (Get-Command node.exe -ErrorAction SilentlyContinue).Source
    if (-not $node) { throw "Node.js was not found after installation." }
  }
}
if ($node) {
  $nodeVersion = (& $node --version).TrimStart("v")
  $nodeMajor = [int]($nodeVersion.Split(".")[0])
  if ($nodeMajor -lt 22) { throw "Node.js 22 or newer is required; found $nodeVersion." }
  Write-Ok "Node.js: $node ($nodeVersion)"
}
$npm = if ($node) { (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source } else { $null }
if ($node -and -not $npm) { throw "npm.cmd was not found beside Node.js." }
$ffmpeg = (Get-Command ffmpeg.exe -ErrorAction SilentlyContinue).Source
if (-not $ffmpeg -and $InstallPrerequisites) {
  Install-WingetPackage "Gyan.FFmpeg" "FFmpeg"
  $ffmpeg = (Get-Command ffmpeg.exe -ErrorAction SilentlyContinue).Source
}
if ($ffmpeg) { Write-Ok "FFmpeg: $ffmpeg" } else { Write-Warn "FFmpeg is missing. Video processing may fail."; $missingPrerequisites += "FFmpeg" }

function Find-MaiBotRoot([string]$Requested) {
  if ($Requested) {
    $resolved = [IO.Path]::GetFullPath($Requested)
    if (Test-Path -LiteralPath (Join-Path $resolved "modules\MaiBot")) { return (Join-Path $resolved "modules\MaiBot") }
    if (Test-Path -LiteralPath (Join-Path $resolved "config")) { return $resolved }
  }
  $base = Join-Path $env:APPDATA "MaiBotOneKeyDesktop"
  if (-not (Test-Path -LiteralPath $base)) { return $null }
  $candidates = Get-ChildItem -LiteralPath $base -Directory -ErrorAction SilentlyContinue |
    Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "modules\MaiBot") } |
    Sort-Object LastWriteTime -Descending
  if ($candidates) { return (Join-Path $candidates[0].FullName "modules\MaiBot") }
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
  $candidate = Get-ChildItem -LiteralPath (Join-Path $Root ".onekey\environments") -Directory -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    ForEach-Object { Join-Path $_.FullName "python.exe" } |
    Where-Object { Test-Path -LiteralPath $_ } |
    Select-Object -First 1
  return $candidate
}

function Find-MaiBotExe([string]$Requested) {
  if ($Requested -and (Test-Path -LiteralPath $Requested)) { return $Requested }
  $running = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -eq "MaiBot OneKey.exe" -and $_.ExecutablePath } |
    Select-Object -First 1
  if ($running) { return $running.ExecutablePath }
  $programs = Join-Path $env:LOCALAPPDATA "Programs"
  if (Test-Path -LiteralPath $programs) {
    return Get-ChildItem -LiteralPath $programs -Filter "MaiBot OneKey.exe" -File -Recurse -Depth 4 -ErrorAction SilentlyContinue |
      Select-Object -First 1 -ExpandProperty FullName
  }
  return $null
}

Write-Step "Checking MaiBot OneKey"
$resolvedMaiBotRoot = Find-MaiBotRoot $MaiBotRoot
$resolvedMaiBotPython = Find-MaiBotPython $resolvedMaiBotRoot
$resolvedMaiBotExe = Find-MaiBotExe $MaiBotExe
if ($resolvedMaiBotRoot) { Write-Ok "MaiBot root: $resolvedMaiBotRoot" } else { Write-Warn "MaiBot OneKey config was not found. QQ and memory features require it." }
if ($resolvedMaiBotPython) { Write-Ok "MaiBot Python: $resolvedMaiBotPython" } else { Write-Warn "MaiBot Python environment was not found." }
if ($resolvedMaiBotExe) { Write-Ok "MaiBot executable: $resolvedMaiBotExe" } else { Write-Warn "MaiBot executable was not found; start it manually." }

Write-Step "Deployment plan"
Write-Host "  BiliMemory root : $InstallRoot"
Write-Host "  Python venv     : $venvDir"
Write-Host "  Vector service  : $embeddingDir"
Write-Host "  Admin API       : http://127.0.0.1:8765"
Write-Host "  Memory scope    : $MemoryScope"
Write-Host "  DSH plugin name : dsh-bilimemory"

if ($DryRun) {
  if ($missingPrerequisites.Count -gt 0) { Write-Warn "Missing prerequisites: $($missingPrerequisites -join ', ')" }
  Write-Host "`nDry run completed. No files or environment variables were changed." -ForegroundColor Yellow
  exit 0
}

Write-Step "Creating Python environment and installing dependencies"
if (-not (Test-Path -LiteralPath $venvPython)) {
  & $python -m venv $venvDir
  if ($LASTEXITCODE -ne 0) { throw "Failed to create the Python virtual environment." }
}
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Failed to upgrade pip." }
& $venvPython -m pip install -r (Join-Path $InstallRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Failed to install Python requirements." }
if (-not $SkipPlaywrightBrowser) {
  & $venvPython -m playwright install chromium
  if ($LASTEXITCODE -ne 0) { Write-Warn "Playwright Chromium installation failed; an existing Edge installation may still work." }
}

Write-Step "Installing the local embedding service"
Push-Location $embeddingDir
try {
  & $npm ci
  if ($LASTEXITCODE -ne 0) { throw "npm ci failed in local-embedding." }
} finally { Pop-Location }

Write-Step "Creating configuration files"
$dataDir = Join-Path $appDir "data"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
$biliConfigPath = Join-Path $appDir "config.json"
$biliConfigExisted = Test-Path -LiteralPath $biliConfigPath
$copies = @(
  @((Join-Path $appDir "config.example.json"), (Join-Path $appDir "config.json")),
  @((Join-Path $appDir "examples\mention_allowlist.example.json"), (Join-Path $appDir "mention_allowlist.json")),
  @((Join-Path $appDir "examples\qq_user_allowlist.example.json"), (Join-Path $dataDir "qq_user_allowlist.json")),
  @((Join-Path $appDir "examples\persona.example.md"), (Join-Path $appDir "persona.md"))
)
foreach ($pair in $copies) {
  if (-not (Test-Path -LiteralPath $pair[1])) { Copy-Item -LiteralPath $pair[0] -Destination $pair[1] }
}

$biliConfig = Get-Content -LiteralPath $biliConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$memoryScopeExplicit = $PSBoundParameters.ContainsKey("MemoryScope") -or [bool]$MemoryChatId
if ($biliConfigExisted -and -not $memoryScopeExplicit) {
  $effectiveMemoryScope = if ($biliConfig.memory_scope -eq "chat") { "chat" } else { "global" }
} else {
  $biliConfig.memory_scope = $MemoryScope
  $effectiveMemoryScope = $MemoryScope
}
if ($MemoryChatId) {
  $biliConfig.chat_id = $MemoryChatId
}
if ($effectiveMemoryScope -eq "chat" -and -not ([string]$biliConfig.chat_id).Trim()) {
  throw "Chat-scoped memory requires a valid A-Memorix chat_id. Pass -MemoryChatId <id> or use -MemoryScope global."
}
if (-not $biliConfigExisted -or $memoryScopeExplicit) {
  [IO.File]::WriteAllText($biliConfigPath, ($biliConfig | ConvertTo-Json -Depth 30), (New-Object Text.UTF8Encoding($false)))
}

Write-Step "Configuring A-Memorix memory"
$memoryReady = $false
$memoryInstallAttempted = $false
$memoryPackageVersion = $null
if ($SkipMemoryConfiguration) {
  Write-Warn "A-Memorix configuration was skipped."
} elseif ($resolvedMaiBotRoot -and $resolvedMaiBotPython) {
  $configurator = Join-Path $InstallRoot "tools\configure_memory.py"
  $memoryArgs = @(
    $configurator,
    "--mai-bot-root", $resolvedMaiBotRoot,
    "--embedding-url", "http://127.0.0.1:11435/v1",
    "--model-name", "local-bge-small-zh",
    "--model-id", "bge-small-zh-v1.5",
    "--dimension", "512"
  )

  Write-Host "  Checking A-Memorix..."
  $memoryOutput = & $resolvedMaiBotPython @memoryArgs 2>&1
  if ($LASTEXITCODE -ne 0) {
    Write-Warn "A-Memorix check failed: $($memoryOutput -join ' ')"
  } else {
    try {
      $memoryResult = ($memoryOutput -join "`n") | ConvertFrom-Json
      if (-not [bool]$memoryResult.aMemorixInstalled) {
        Write-Warn "A-Memorix is missing. MaiBot 1.3.15 or newer provides this module."
        if ($SkipAMemorixInstall) {
          Write-Warn "Direct A-Memorix installation was skipped by -SkipAMemorixInstall."
        } else {
          Write-Step "Installing A-Memorix through MaiBot"
          Write-Host "  Package: $AMemorixPackageSpec"
          Write-Host "  Python : $resolvedMaiBotPython"
          $memoryInstallAttempted = $true
          $installer = Join-Path $InstallRoot "tools\install_amemorix.py"
          $installOutput = & $resolvedMaiBotPython $installer --package-spec $AMemorixPackageSpec --apply 2>&1
          $installExit = $LASTEXITCODE
          try {
            $installResult = ($installOutput -join "`n") | ConvertFrom-Json
            $memoryPackageVersion = $installResult.packageVersion
            if ($installResult.output) {
              Write-Host ($installResult.output.TrimEnd())
            }
            if ($installExit -ne 0 -or -not [bool]$installResult.aMemorixInstalled) {
              throw [string]$installResult.error
            }
            Write-Ok "MaiBot $($installResult.packageVersion) installed; A-Memorix detected."
          } catch {
            Write-Warn "Direct A-Memorix installation failed: $($_.Exception.Message)"
            Write-Warn "Close MaiBot OneKey, rerun install.cmd, or install manually with:"
            Write-Warn "  & `"$resolvedMaiBotPython`" -m pip install --upgrade `"$AMemorixPackageSpec`""
          }
        }
      }

      if (-not $memoryPackageVersion) {
        $memoryPackageVersion = & $resolvedMaiBotPython -c "import importlib.metadata; print(importlib.metadata.version('MaiBot'))" 2>$null
      }
      $memoryOutput = & $resolvedMaiBotPython @memoryArgs --apply 2>&1
      if ($LASTEXITCODE -ne 0) {
        Write-Warn "A-Memorix configuration failed: $($memoryOutput -join ' ')"
      } else {
        $memoryResult = ($memoryOutput -join "`n") | ConvertFrom-Json
        $memoryReady = [bool]$memoryResult.aMemorixInstalled -and [bool]$memoryResult.applied
        if ($memoryReady) {
          Write-Ok "A-Memorix enabled with local-bge-small-zh (512 dimensions)."
        } else {
          Write-Warn "A-Memorix is still unavailable. Update MaiBot OneKey, then rerun install.cmd."
        }
      }
    } catch {
      Write-Warn "A-Memorix configuration returned an unreadable result: $($_.Exception.Message)"
    }
  }
} else {
  Write-Warn "MaiBot was not detected; A-Memorix configuration and installation were skipped."
}
$envLines = @(
  "BILIMEMORY_ROOT=$InstallRoot",
  "BILIMEMORY_ADMIN_URL=http://127.0.0.1:8765",
  "BILIMEMORY_MEDIA_PYTHON=$venvPython",
  "MAIBOT_ROOT=$resolvedMaiBotRoot",
  "MAIBOT_PYTHON=$resolvedMaiBotPython",
  "MAIBOT_EXE=$resolvedMaiBotExe",
  "BILIMEMORY_MEMORY_SCOPE=$effectiveMemoryScope",
  "BILIMEMORY_ENABLE_QQ=1"
)
[IO.File]::WriteAllLines((Join-Path $InstallRoot ".env.local"), $envLines, (New-Object Text.UTF8Encoding($false)))

$userVariables = @{
  BILIMEMORY_ROOT = $InstallRoot
  BILIMEMORY_ADMIN_URL = "http://127.0.0.1:8765"
  BILIMEMORY_MEDIA_PYTHON = $venvPython
  MAIBOT_ROOT = $resolvedMaiBotRoot
  MAIBOT_PYTHON = $resolvedMaiBotPython
  MAIBOT_EXE = $resolvedMaiBotExe
}
foreach ($entry in $userVariables.GetEnumerator()) {
  if ($entry.Value) { [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, "User") }
}

Write-Step "Creating desktop shortcut"
try {
  $desktop = [Environment]::GetFolderPath("Desktop")
  $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desktop "BiliMemory 一键启动.lnk"))
  $shortcut.TargetPath = Join-Path $InstallRoot "start-all.cmd"
  $shortcut.WorkingDirectory = $InstallRoot
  $shortcut.Description = "Start BiliMemory backend and open the management panel"
  $shortcut.WindowStyle = 1
  $shortcut.Save()
} catch { Write-Warn "Could not create the desktop shortcut: $($_.Exception.Message)" }

if (-not $NoStart) {
  Write-Step "Starting BiliMemory services"
  & (Join-Path $InstallRoot "tools\start-backend.ps1") -StartMaiBot -WithQQ -WithBiliDM -OpenPanel
}

Write-Host "`nInstallation completed." -ForegroundColor Green
Write-Host "DSH Desktop: open Plugins -> Install plugin and enter github:binbin-QAQ/BiliMemory"
Write-Host "Management panel: http://127.0.0.1:8765"
Write-Host "If Bilibili login is missing: $venvPython bilibili_tool.py login"
Write-Host "Memory scope: $effectiveMemoryScope"
Write-Host "Memory ready: $memoryReady"
Write-Host "Memory install attempted: $memoryInstallAttempted"
Write-Host "MaiBot version: $memoryPackageVersion"
Write-Host "Run the desktop shortcut later to start all services."
