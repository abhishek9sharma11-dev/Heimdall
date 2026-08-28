#!/usr/bin/env bash
# hermes_slots.sh — start/stop/status for multi-webinar stacks (one port = one session)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORTS=(8765 8766 8767 8768 8769 8770 8771 8772 8773 8774 8775)
PIDDIR="/tmp/hermes-slots"
mkdir -p "$PIDDIR"

usage() {
  cat <<EOF
Usage:
  $0 status
  $0 start <port> <env_file>     # e.g. start 8768 .env.82260231356
  $0 stop <port>                 # kills bridge+python for that port only
  $0 logs <port>

Ports: ${PORTS[*]}
EOF
}

need_port() {
  local p="${1:-}"
  [[ "$p" =~ ^(876[5-9]|877[0-5])$ ]] || { echo "port must be one of: ${PORTS[*]}"; exit 1; }
}

status() {
  echo "Hermes slots @ $(date '+%H:%M:%S')"
  printf "%-6s %-8s %-14s %-8s %s\n" "PORT" "BRIDGE" "MEETING" "PYTHON" "PIDS"
  for p in "${PORTS[@]}"; do
    local health state py bp pp
    bp=""; pp=""; state="-"; py="down"
    [[ -f "$PIDDIR/bridge-${p}.pid" ]] && bp="$(cat "$PIDDIR/bridge-${p}.pid" 2>/dev/null || true)"
    [[ -f "$PIDDIR/python-${p}.pid" ]] && pp="$(cat "$PIDDIR/python-${p}.pid" 2>/dev/null || true)"
    health="$(curl --noproxy '*' -s -m 1 "http://127.0.0.1:${p}/health" 2>/dev/null || true)"
    if [[ -z "$health" ]]; then
      printf "%-6s %-8s %-14s %-8s b=%s p=%s\n" "$p" "down" "-" "down" "${bp:-—}" "${pp:-—}"
      continue
    fi
    state="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('meeting_state') or '?')" "$health" 2>/dev/null || echo "?")"
    if [[ -n "$pp" ]] && kill -0 "$pp" 2>/dev/null; then
      py="up"
    else
      py="?"
    fi
    printf "%-6s %-8s %-14s %-8s b=%s p=%s\n" "$p" "up" "$state" "$py" "${bp:-—}" "${pp:-—}"
  done
}

stop_port() {
  local p="$1"
  need_port "$p"
  echo "Stopping slot :${p} …"
  local bp pp
  bp="$(cat "$PIDDIR/bridge-${p}.pid" 2>/dev/null || true)"
  pp="$(cat "$PIDDIR/python-${p}.pid" 2>/dev/null || true)"
  if [[ -n "$pp" ]]; then
    kill "$pp" 2>/dev/null || true
    sleep 0.3
    kill -9 "$pp" 2>/dev/null || true
    rm -f "$PIDDIR/python-${p}.pid"
    echo "  python $pp stopped"
  fi
  if [[ -n "$bp" ]]; then
    kill "$bp" 2>/dev/null || true
    sleep 0.3
    kill -9 "$bp" 2>/dev/null || true
    rm -f "$PIDDIR/bridge-${p}.pid"
    echo "  bridge $bp stopped"
  fi
  # Fallback: anything still listening on the port
  local listen
  listen="$(lsof -nP -tiTCP:"$p" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "$listen" ]]; then
    # shellcheck disable=SC2086
    kill $listen 2>/dev/null || true
    sleep 0.2
    # shellcheck disable=SC2086
    kill -9 $listen 2>/dev/null || true
    echo "  cleared listeners on :${p}"
  fi
  echo "Done :${p}"
}

start_port() {
  local p="$1"
  local envf="$2"
  need_port "$p"
  local envpath
  if [[ -f "$envf" ]]; then
    envpath="$(cd "$(dirname "$envf")" && pwd)/$(basename "$envf")"
  elif [[ -f "$ROOT/$envf" ]]; then
    envpath="$ROOT/$envf"
  else
    echo "env file not found: $envf"; exit 1
  fi

  if curl --noproxy '*' -s -m 1 "http://127.0.0.1:${p}/health" >/dev/null 2>&1; then
    echo "Port ${p} already has a bridge. Run: $0 stop ${p}"
    exit 1
  fi

  if ! grep -q "BRIDGE_URL=.*:${p}" "$envpath"; then
    echo "ERROR: $envpath must set BRIDGE_URL=http://127.0.0.1:${p}"
    exit 1
  fi

  echo "Starting bridge :${p} …"
  (
    cd "$ROOT/bridge/node-bridge"
    # Render's Playwright image installs Chromium under /ms-playwright. Local
    # runs may have a machine-specific cache, so only force the image path in
    # the container runtime.
    if [[ "${HERMES_DOCKER:-0}" == "1" ]]; then
      export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/ms-playwright}"
    else
      unset PLAYWRIGHT_BROWSERS_PATH
    fi
    nohup env BRIDGE_PORT="$p" node index.js >>"/tmp/node-bridge-${p}.log" 2>&1 &
    echo $! >"$PIDDIR/bridge-${p}.pid"
  )
  bridge_ready=0
  for _ in $(seq 1 10); do
    if curl --noproxy '*' -s -m 2 "http://127.0.0.1:${p}/health" >/dev/null; then
      bridge_ready=1
      break
    fi
    sleep 1
  done
  if [[ "$bridge_ready" != "1" ]]; then
    echo "bridge failed — see /tmp/node-bridge-${p}.log"
    tail -n 40 "/tmp/node-bridge-${p}.log" 2>/dev/null || true
    exit 1
  fi
  echo "Bridge OK (pid $(cat "$PIDDIR/bridge-${p}.pid"))"

  echo "Starting python with $(basename "$envpath") …"
  PYTHON_BIN="${PYTHON:-python3}"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT/.venv/bin/python"
fi

nohup bash -lc "
  cd '$ROOT'
  set -a
  source '$envpath'
  set +a
  export ZOOM_BACKEND=bridge
  exec '$PYTHON_BIN' -m src.main
" >>"/tmp/python-${p}.log" 2>&1 &
  echo $! >"$PIDDIR/python-${p}.pid"
  sleep 1
  if ! kill -0 "$(cat "$PIDDIR/python-${p}.pid")" 2>/dev/null; then
    echo "python failed to stay up — see /tmp/python-${p}.log"
    tail -n 40 "/tmp/python-${p}.log" 2>/dev/null || true
    exit 1
  fi
  echo "Python OK (pid $(cat "$PIDDIR/python-${p}.pid")) → /tmp/python-${p}.log"
}

logs_port() {
  local p="$1"
  need_port "$p"
  echo "=== /tmp/node-bridge-${p}.log (tail) ==="
  tail -n 40 "/tmp/node-bridge-${p}.log" 2>/dev/null || echo "(missing)"
  echo "=== /tmp/python-${p}.log (tail) ==="
  tail -n 40 "/tmp/python-${p}.log" 2>/dev/null || echo "(missing)"
}

cmd="${1:-}"
case "$cmd" in
  status) status ;;
  start) start_port "${2:-}" "${3:-}" ;;
  stop) stop_port "${2:-}" ;;
  logs) logs_port "${2:-}" ;;
  *) usage; exit 1 ;;
esac
