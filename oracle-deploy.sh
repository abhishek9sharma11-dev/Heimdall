#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
docker compose -f docker-compose.oracle.yml up -d --build
docker compose -f docker-compose.oracle.yml ps
curl -fsS http://127.0.0.1:8780/health
