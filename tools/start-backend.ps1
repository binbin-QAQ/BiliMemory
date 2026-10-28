[CmdletBinding()]
param(
  [switch]$StartMaiBot,
  [switch]$WithQQ,
  [switch]$WithBiliDM,
  [switch]$OpenPanel
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$envFile = Join-Path $root ".env.local"
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Import-EnvFile([string]$Path) {
  if (-not (Test-Path -LiteralPath $Path)) { return }
  foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
    $trim = $line.Trim()
    if (-not $trim -or $trim.StartsWith("#") -or -not $trim.Contains("=")) { continue }
    $key, $value = $trim.Split("=", 2)
    [Environment]::SetEnvironmentVariable($key.Trim(), $value.Trim(), "Process")
  }
}

function Test-Port([int]$Port) {
  return [bool](Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
}

function Wait-Port([int]$Port, [int]$Seconds = 45) {
  $deadline = (Get-Date).AddSeconds($Seconds)
  while ((Get-Date) -lt $deadline) {
    if (Test-Port $Port) { return $true }
    Start-Sleep -Milliseconds 500
  }
  return $false
}

function Start-Hidden([string]$File, [string]$Arguments, [string]$WorkingDirectory, [string]$Name) {
  $stdout = Join-Path $logDir "$Name.stdout.log"
  $stderr = Join-Path $logDir "$Name.stderr.log"
  Start-Process -FilePath $File -ArgumentList $Arguments -WorkingDirectory $WorkingDirectory -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr | Out-Null
}

Import-EnvFile $envFile
$node = (Get-Command node.exe -ErrorAction SilentlyContinue).Source
$mediaPython = if ($env:BILIMEMORY_MEDIA_PYTHON) { $env:BILIMEMORY_MEDIA_PYTHON } else { (Join-Path $root ".venv\Scripts\python.exe") }
$maibotPython = $env:MAIBOT_PYTHON
if (-not $node) { throw "Node.js was not found. Run install.ps1 first." }
if (-not (Test-Path -LiteralPath $mediaPython)) { throw "Python environment not found: $mediaPython. Run install.ps1 first." }

if ($StartMaiBot -and -not (Test-Port 8001)) {
  $maibotExe = $env:MAIBOT_EXE
  if (-not $maibotExe -or -not (Test-Path -LiteralPath $maibotExe)) {
    Write-Warning "MaiBot OneKey executable was not found. Start it manually, then run this command again."
  } else {
    Start-Process -FilePath $maibotExe -ArgumentList "--remote-debugging-port=9327" -WorkingDirectory (Split-Path -Parent $maibotExe) | Out-Null
    if (Wait-Port 9327 90) {
      & $node (Join-Path $root "tools\start-maibot-services.mjs") 9327
      if ($LASTEXITCODE -ne 0) { throw "MaiBot services failed to start." }
    } else {
      throw "MaiBot launcher did not expose its local control port."
    }
  }
}

if (-not (Test-Port 11435)) {
  $embeddingDir = Join-Path $root "local-embedding"
  if (-not (Test-Path -LiteralPath (Join-Path $embeddingDir "node_modules"))) { throw "local-embedding dependencies are missing. Run install.ps1 first." }
  Start-Hidden $node "server.mjs" $embeddingDir "embedding"
}

if (-not (Test-Port 8765)) {
  $adminDir = Join-Path $root "bilibili-memory\admin"
  $env:ADMIN_NO_OPEN = "1"
  $env:PYTHONIOENCODING = "utf-8"
  $env:PYTHONUTF8 = "1"
  Start-Hidden $node "server.mjs" $adminDir "admin"
}

if ($WithQQ -and (Test-Port 7998) -and -not (Test-Port 8766)) {
  $appDir = Join-Path $root "bilibili-memory"
  Start-Hidden $mediaPython "qq_link_watcher.py" $appDir "qq-watcher"
}

if ($WithBiliDM -and -not (Test-Port 8768)) {
  $appDir = Join-Path $root "bilibili-memory"
  if (Test-Path -LiteralPath (Join-Path $appDir "storage_state.json")) {
    Start-Hidden $mediaPython "bili_dm_service.py --run" $appDir "bili-dm"
  }
}

$required = 8765, 11435
if ($StartMaiBot) { $required += 8001 }
if ($WithQQ) { $required += 7998, 8766 }
if ($WithBiliDM) { $required += 8768 }
$failed = @()
foreach ($port in $required) {
  if (-not (Wait-Port $port 60)) { $failed += $port }
}

Get-NetTCPConnection -State Listen -LocalPort 7998,8001,8765,8766,8768,11435 -ErrorAction SilentlyContinue |
  Sort-Object LocalPort |
  Select-Object LocalAddress, LocalPort, OwningProcess, State |
  Format-Table -AutoSize

if ($failed.Count -gt 0) {
  throw "These required ports did not become ready: $($failed -join ', '). Check logs under $logDir."
}

if ($OpenPanel) {
  Start-Process "http://127.0.0.1:8765"
}
Write-Host "BiliMemory backend is ready: http://127.0.0.1:8765" -ForegroundColor Green