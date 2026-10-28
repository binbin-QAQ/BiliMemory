[CmdletBinding()]
param([switch]$IncludeMaiBot)

$ErrorActionPreference = "SilentlyContinue"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$node = (Get-Command node.exe -ErrorAction SilentlyContinue).Source
if ($IncludeMaiBot -and $node) {
  & $node (Join-Path $root "tools\stop-maibot-services.mjs") 9327
}
foreach ($port in 8765,8766,8768,11435) {
  Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
}
Write-Host "BiliMemory backend stopped." -ForegroundColor Green