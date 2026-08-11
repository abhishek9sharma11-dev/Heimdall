#!/usr/bin/env bash
set -euo pipefail

# Run the Heimdall AI webinar bot.
# By default this uses Docker Compose if Docker is installed.
# If Docker is not available, it will attempt a local native run.

cd "$(dirname "$0")"

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  echo "Starting bot with Docker Compose..."
  docker compose up -d --build
  echo "Bot started. Use ./stop.sh to stop it."
  exit 0
fi

if command -v python3.10 >/dev/null 2>&1; then
  echo "Docker not found. Starting native Python run."
  if [ ! -d ".venv" ]; then
    python3.10 -m venv .venv
  fi
  source .venv/bin/activate
  pip install -r requirements.txt

  echo "Building C++ bridge..."
  mkdir -p bridge/build
  pushd bridge/build >/dev/null
  cmake ..
  make -j
  popd >/dev/null

  echo "Starting bridge in background..."
  ./bridge/build/zoom-bridge &
  BRIDGE_PID=$!
  echo "Bridge PID=$BRIDGE_PID"

  echo "Waiting for bridge to become ready..."
  for i in $(seq 1 20); do
    if curl -sf http://127.0.0.1:8765/health >/dev/null 2>&1; then
      echo "Bridge ready."
      break
    fi
    sleep 1
  done

  echo "Starting Python orchestrator..."
  ZOOM_BACKEND=bridge python3.10 -m src.main
  exit 0
fi

echo "ERROR: Neither Docker nor python3.10 was found on PATH."
exit 1
