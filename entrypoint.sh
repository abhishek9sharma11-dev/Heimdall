#!/bin/bash
set -e

# SDK Qt and native libs need to be on the library path
export LD_LIBRARY_PATH="/app/bridge/build:/app/bridge/build/qt_libs/Qt/lib:${LD_LIBRARY_PATH}"
export HOME=/root
mkdir -p /root/.zoom

# The Zoom SDK requires a display even in headless/chat-only mode
rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1024x768x16 &
export DISPLAY=:99
sleep 1

echo "[entrypoint] Starting Zoom bridge..."
/app/bridge/build/zoom-bridge &
BRIDGE_PID=$!

# Wait for bridge to be ready
for i in $(seq 1 20); do
    if curl -sf http://127.0.0.1:8765/health > /dev/null 2>&1; then
        echo "[entrypoint] Bridge ready."
        break
    fi
    sleep 1
done

echo "[entrypoint] Starting Python orchestrator..."
cd /app
exec python3.10 -m src.main
