# Removes Windows TCP port exclusions that overlap Docker’s old default host mapping 127.0.0.1:51000-51515
# (compose now defaults to 58000-58515 to avoid many Hyper-V exclusions; this script still helps if you map lower ports).
#
# If plain netsh delete returns "Access is denied" even as Admin, Windows often holds those
# ranges while WinNAT is running. Try:
#   .\free-docker-proxy-ports.ps1 -StopWinNatFirst
# (Brief disruption to Hyper-V/WSL2 NAT; Docker Desktop may reconnect after.)
#
# Usage (elevated PowerShell — right-click -> Run as administrator):
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
#   cd E:\FB\protonusa\scripts
#   .\free-docker-proxy-ports.ps1
#   .\free-docker-proxy-ports.ps1 -StopWinNatFirst
#
# If netsh show lists different Start/End ports later, edit the deletes below to match.

param(
    [switch] $StopWinNatFirst
)

$ErrorActionPreference = "Stop"

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "This script must run in an elevated PowerShell (Run as administrator)." -ForegroundColor Red
    Write-Host "Tip: Start menu -> type PowerShell -> right-click -> Run as administrator." -ForegroundColor Yellow
    exit 1
}

Write-Host "Before:" -ForegroundColor Cyan
netsh interface ipv4 show excludedportrange protocol=tcp

if ($StopWinNatFirst) {
    Write-Host "`nStopping WinNAT (needed so some exclusions can be removed)..." -ForegroundColor Yellow
    net stop winnat 2>&1 | ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "net stop winnat failed (exit $LASTEXITCODE). Try closing Docker Desktop / WSL, then run again." -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

try {
    netsh interface ipv4 delete excludedportrange protocol=tcp startport=51356 numberofports=100
    netsh interface ipv4 delete excludedportrange protocol=tcp startport=51456 numberofports=100
} finally {
    if ($StopWinNatFirst) {
        Write-Host "`nStarting WinNAT again..." -ForegroundColor Yellow
        net start winnat 2>&1 | ForEach-Object { Write-Host $_ }
    }
}

Write-Host "`nAfter:" -ForegroundColor Cyan
netsh interface ipv4 show excludedportrange protocol=tcp

Write-Host "`nDone. From repository root run: docker compose up -d" -ForegroundColor Green
