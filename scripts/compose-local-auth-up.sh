#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -z "${IXBROWSER_WINDOWS_HOST:-}" ]] && grep -qi microsoft /proc/version 2>/dev/null; then
  detected="$(ip route show default 2>/dev/null | awk '{print $3; exit}')"
  if [[ -n "$detected" ]]; then
    export IXBROWSER_WINDOWS_HOST="$detected"
    echo "Exported IXBROWSER_WINDOWS_HOST=$IXBROWSER_WINDOWS_HOST for WSL Docker"
  fi
fi

exec docker compose -f docker-compose.local-auth.yml up -d --build "$@"
