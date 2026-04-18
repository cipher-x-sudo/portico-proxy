# Removes Windows TCP port exclusions that overlap the host port range Docker binds (default 58000-58515).
# Hyper-V / WinNAT can reserve ranges that block LISTEN on 127.0.0.1; deleting overlapping exclusions fixes
# "bind: An attempt was made to access a socket in a way forbidden by its access permissions".
#
# If plain netsh delete returns "Access is denied" even as Admin, Windows often holds those
# ranges while WinNAT is running. Try:
#   .\free-docker-proxy-ports.ps1 -StopWinNatFirst
# (Brief disruption to Hyper-V/WSL2 NAT; Docker Desktop may reconnect after.)
#
# Usage (elevated PowerShell — right-click -> Run as administrator):
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
#   cd <repo>\scripts
#   .\free-docker-proxy-ports.ps1
#   .\free-docker-proxy-ports.ps1 -StopWinNatFirst
#   .\free-docker-proxy-ports.ps1 -PortFirst 58000 -PortLast 58515 -StopWinNatFirst
#
# Match -PortFirst/-PortLast to DOCKER_PROXY_HOST_PORT_FIRST / DOCKER_PROXY_HOST_LAST in .env if you override them.

param(
    [switch] $StopWinNatFirst,
    [int] $PortFirst = 58000,
    [int] $PortLast = 58515
)

$ErrorActionPreference = "Stop"

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "This script must run in an elevated PowerShell (Run as administrator)." -ForegroundColor Red
    Write-Host "Tip: Start menu -> type PowerShell -> right-click -> Run as administrator." -ForegroundColor Yellow
    exit 1
}

if ($PortLast -lt $PortFirst) {
    Write-Host "-PortLast must be >= -PortFirst." -ForegroundColor Red
    exit 1
}

function Get-TcpExcludedPortRanges {
    $out = netsh interface ipv4 show excludedportrange protocol=tcp 2>&1 | Out-String
    $ranges = @()
    foreach ($line in ($out -split "`r?`n")) {
        if ($line -match '^\s*(\d+)\s+(\d+)\s*$') {
            $s = [int]$Matches[1]
            $e = [int]$Matches[2]
            $ranges += [pscustomobject]@{ Start = $s; End = $e }
        }
    }
    return $ranges
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
    $ranges = Get-TcpExcludedPortRanges
    $toDelete = @()
    foreach ($r in $ranges) {
        if ($r.Start -le $PortLast -and $r.End -ge $PortFirst) {
            $toDelete += $r
        }
    }
    if ($toDelete.Count -eq 0) {
        Write-Host "`nNo TCP excluded port ranges overlap ${PortFirst}-${PortLast} (nothing to delete)." -ForegroundColor Yellow
    } else {
        Write-Host "`nDeleting excluded ranges that overlap ${PortFirst}-${PortLast}..." -ForegroundColor Yellow
        foreach ($r in $toDelete) {
            $n = $r.End - $r.Start + 1
            Write-Host "  delete startport=$($r.Start) numberofports=$n" -ForegroundColor Gray
            cmd /c "netsh interface ipv4 delete excludedportrange protocol=tcp startport=$($r.Start) numberofports=$n" 2>&1 | ForEach-Object { Write-Host $_ }
            if ($LASTEXITCODE -ne 0) {
                Write-Host "netsh delete failed for $($r.Start)-$($r.End) (exit $LASTEXITCODE)." -ForegroundColor Red
                exit $LASTEXITCODE
            }
        }
    }

    # Legacy defaults (51000-51515) — safe no-ops if absent
    netsh interface ipv4 delete excludedportrange protocol=tcp startport=51356 numberofports=100 2>$null
    netsh interface ipv4 delete excludedportrange protocol=tcp startport=51456 numberofports=100 2>$null
} finally {
    if ($StopWinNatFirst) {
        Write-Host "`nStarting WinNAT again..." -ForegroundColor Yellow
        net start winnat 2>&1 | ForEach-Object { Write-Host $_ }
    }
}

Write-Host "`nAfter:" -ForegroundColor Cyan
netsh interface ipv4 show excludedportrange protocol=tcp

Write-Host "`nDone. From repository root run: docker compose up -d" -ForegroundColor Green
