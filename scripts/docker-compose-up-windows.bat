@echo off
setlocal EnableExtensions
cd /d "%~dp0.."

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Requesting Administrator rights to remove Windows TCP exclusions that block Docker binds...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b 1
)

set "PORT_FIRST=%~1"
set "PORT_LAST=%~2"
if "%PORT_FIRST%"=="" set "PORT_FIRST=58000"
if "%PORT_LAST%"=="" set "PORT_LAST=58515"

echo Repo root: %CD%
echo Freeing excluded TCP ranges overlapping %PORT_FIRST%-%PORT_LAST% ^(match docker-compose / .env host proxy ports^)...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0free-docker-proxy-ports.ps1" -StopWinNatFirst -PortFirst %PORT_FIRST% -PortLast %PORT_LAST%
if errorlevel 1 exit /b %errorLevel%

echo.
echo Starting stack...
docker compose up -d
if errorlevel 1 exit /b %errorLevel%

echo.
echo Done. Logs: docker compose logs -f
exit /b 0
