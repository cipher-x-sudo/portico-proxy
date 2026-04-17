#!/bin/bash
set -e
MARKER="Initialization Sequence Completed"
LOG=/tmp/openvpn.log
AUTH=/tmp/auth
TIMEOUT=120

if [ -z "$OVPN_FILE" ]; then
  echo "OVPN_FILE is required" >&2
  exit 1
fi
if [ ! -f "/ovpn/$OVPN_FILE" ]; then
  echo "OVPN file not found: /ovpn/$OVPN_FILE" >&2
  exit 1
fi

if [ -n "$AUTH_USER" ]; then
  printf '%s\n%s' "$AUTH_USER" "$AUTH_PASS" > "$AUTH"
  chmod 600 "$AUTH"
  AUTH_ARGS=(--auth-user-pass "$AUTH")
else
  AUTH_ARGS=()
fi

openvpn --config "/ovpn/$OVPN_FILE" "${AUTH_ARGS[@]}" --log "$LOG" --daemon
deadline=$((SECONDS + TIMEOUT))
while [ $SECONDS -lt $deadline ]; do
  if [ -f "$LOG" ] && grep -q "$MARKER" "$LOG" 2>/dev/null; then
    break
  fi
  sleep 0.5
done
if ! grep -q "$MARKER" "$LOG" 2>/dev/null; then
  echo "OpenVPN did not become ready within ${TIMEOUT}s" >&2
  cat "$LOG" >&2
  exit 1
fi

# HTTP proxy only on 8080
if [ -n "$PROXY_USER" ] && [ -n "$PROXY_PASS" ]; then
  exec python3 -m pproxy -l "http://0.0.0.0:8080#${PROXY_USER}:${PROXY_PASS}"
else
  exec python3 -m pproxy -l "http://0.0.0.0:8080"
fi
