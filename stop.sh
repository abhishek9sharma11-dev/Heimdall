#!/usr/bin/env bash
set -euo pipefail

# Stop the Heimdall AI webinar bot.
cd "$(dirname "$0")"

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  echo "Stopping Docker Compose services..."
  docker compose down
  echo "Stopped."
  exit 0
fi

echo "Stopping native local processes..."
pkill -f 'zoom-bridge' || true
pkill -f 'python -m src.main' || true
pkill -f 'ZOOM_BACKEND=bridge' || true

echo "Stopped local bot processes."
