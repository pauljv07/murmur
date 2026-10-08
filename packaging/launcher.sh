#!/bin/bash
# Murmur.app entry point. Starts (or re-opens) the local Murmur server.
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
SUPPORT="$HOME/Library/Application Support/Murmur"
PORT="${MURMUR_PORT:-8765}"
URL="http://127.0.0.1:$PORT/"
mkdir -p "$SUPPORT"

if [ "$(uname -m)" != "arm64" ]; then
  osascript -e 'display alert "Murmur needs a Mac with Apple silicon (M1 or newer)." as critical' >/dev/null
  exit 1
fi

# already running (or setting up): just bring it to the front
if curl -s -m 1 "${URL}api/status" >/dev/null || curl -s -m 1 "${URL}setup/status" >/dev/null; then
  open "$URL"
  exit 0
fi

if [ ! -f "$SUPPORT/.murmur-models" ]; then
  osascript -e 'display notification "Setting up Murmur — a setup page will open in your browser." with title "Murmur"' >/dev/null 2>&1 &
fi

export MURMUR_SUPPORT="$SUPPORT" MURMUR_PORT="$PORT"
export UV_PYTHON_INSTALL_DIR="$SUPPORT/python" UV_CACHE_DIR="$SUPPORT/uv-cache" UV_NO_PROGRESS=1
# after the first setup Murmur runs fully offline (no wifi needed)
if [ -f "$SUPPORT/.murmur-models" ] && [ -f "$SUPPORT/venv/.murmur-lock" ]; then export UV_OFFLINE=1; fi
{ echo; echo "=== $(date) launch ==="; } >> "$SUPPORT/murmur.log"
exec "$RES/uv" run --no-project --python 3.11 --python-preference only-managed --quiet \
  "$RES/bootstrap.py" >> "$SUPPORT/murmur.log" 2>&1
