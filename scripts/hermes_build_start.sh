#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NODE_BRIDGE="$ROOT/bridge/node-bridge"

# ------------------------------------------------------------
# Runtime dependencies
#
# In Render/Docker, dependencies are baked into the image.
# Locally, preserve the existing self-installing behavior.
# ------------------------------------------------------------

if ! command -v node >/dev/null 2>&1; then
  echo "Node.js is required. Install Node.js 18+ and rerun npm run build." >&2
  exit 1
fi

if [ "${HERMES_DOCKER:-0}" != "1" ]; then
  if [ ! -d "$NODE_BRIDGE/node_modules" ]; then
    echo "Installing Playwright bridge dependencies..."
    npm install --prefix "$NODE_BRIDGE"
  fi
fi

if [ "${HERMES_DOCKER:-0}" = "1" ]; then
  PYTHON_BIN="${PYTHON:-python3}"
else
  if [ ! -x "$ROOT/.venv/bin/python" ] && [ -z "${PYTHON:-}" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv "$ROOT/.venv"
  fi

  PYTHON_BIN="${PYTHON:-}"
  if [ -z "$PYTHON_BIN" ] && [ -x "$ROOT/.venv/bin/python" ]; then
    PYTHON_BIN="$ROOT/.venv/bin/python"
  fi
  if [ -z "$PYTHON_BIN" ]; then
    PYTHON_BIN="python3"
  fi
fi

if [ "${HERMES_DOCKER:-0}" != "1" ]; then
  echo "Checking Python dependencies..."
  if ! "$PYTHON_BIN" -c 'import pydantic_settings, httpx' >/dev/null 2>&1; then
    echo "Installing Python dependencies..."
    "$PYTHON_BIN" -m pip install -r "$ROOT/requirements-lite.txt"
  fi
fi

# Install Chromium only when explicitly requested.
if [ "${HERMES_INSTALL_PLAYWRIGHT:-0}" = "1" ]; then
  if ! "$NODE_BRIDGE/node_modules/.bin/playwright" install chromium >/dev/null 2>&1; then
    echo "Warning: Playwright Chromium download failed; the bridge will try installed Chrome." >&2
  fi
fi

"$PYTHON_BIN" -m py_compile "$ROOT/dashboard/server.py" "$ROOT/src/main.py"

export HERMES_HOST="${HERMES_HOST:-0.0.0.0}"
export HERMES_PORT="${HERMES_PORT:-${PORT:-8780}}"
export HERMES_WORKER_MODE="${HERMES_WORKER_MODE:-0}"

echo "Starting Hermes dashboard at http://${HERMES_HOST}:${HERMES_PORT}/connect.html"

cd "$ROOT"

if [ "${HERMES_DISABLE_DAY_OPS:-0}" != "1" ]; then
  # Guard against duplicate watchers.
  if [ -f /tmp/day-ops-runner.pid ]; then
    OLD_PID="$(cat /tmp/day-ops-runner.pid 2>/dev/null || true)"
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
      echo "Stopping previous session watcher (pid $OLD_PID)..."
      kill "$OLD_PID" 2>/dev/null || true
    fi
  fi

  pkill -f "scripts/day_ops_runner.py" 2>/dev/null || true
  sleep 0.5

  echo "Starting automatic session watcher (manifest-configured lead time)..."

  nohup "$PYTHON_BIN" "$ROOT/scripts/day_ops_runner.py" \
    >>/tmp/day-ops-runner.log 2>&1 &

  echo $! > /tmp/day-ops-runner.pid
fi

exec "$PYTHON_BIN" -m dashboard.server
