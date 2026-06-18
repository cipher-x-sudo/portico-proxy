#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Use Docker Desktop when available so containers keep running after this WSL session closes.
if docker context ls --format '{{.Name}}' 2>/dev/null | grep -qx desktop-linux; then
  if [[ "$(docker context show 2>/dev/null || true)" != "desktop-linux" ]]; then
    echo "Switching Docker context to desktop-linux (stack persists after WSL closes)"
    docker context use desktop-linux
  fi
elif grep -qi microsoft /proc/version 2>/dev/null; then
  echo "WARNING: Using WSL's built-in Docker. Containers stop when WSL shuts down."
  echo "  Fix: install Docker Desktop, then run scripts/compose-local-auth-up-windows.bat from PowerShell,"
  echo "  or run: docker context use desktop-linux"
fi

if [[ -z "${IXBROWSER_WINDOWS_HOST:-}" ]] && grep -qi microsoft /proc/version 2>/dev/null; then
  detected="$(ip route show default 2>/dev/null | awk '{print $3; exit}')"
  if [[ -n "$detected" ]]; then
    export IXBROWSER_WINDOWS_HOST="$detected"
    echo "Exported IXBROWSER_WINDOWS_HOST=$IXBROWSER_WINDOWS_HOST for WSL Docker"
  fi
fi

exec docker compose -f docker-compose.local-auth.yml up -d --build "$@"
