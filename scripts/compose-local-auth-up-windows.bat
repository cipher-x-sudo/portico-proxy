@echo off
setlocal EnableExtensions
cd /d "%~dp0.."

echo Repo root: %CD%
echo Starting local-auth stack (Docker Desktop — survives closing WSL)...
echo.

docker context use desktop-linux >nul 2>&1
docker compose -f docker-compose.local-auth.yml up -d --build %*
if errorlevel 1 exit /b %errorLevel%

echo.
echo Done. Dashboard: http://127.0.0.1:8080
echo Logs: docker compose -f docker-compose.local-auth.yml logs -f
exit /b 0
