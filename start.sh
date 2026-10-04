#!/usr/bin/env bash
# Starts all 3 programs in ONE container (on Railway they share one SQLite file).
#   1. simulator  (fake grid)                      - background, demo only
#   2. gateway    (collects + judges the data)     - background
#   3. web portal (map + diagrams on $PORT)        - the public URL
# If any of them dies, the container exits and Railway restarts it.
set -u
PORT="${PORT:-8000}"
export GATEWAY_DB="${GATEWAY_DB:-/data/gateway.db}"
mkdir -p "$(dirname "$GATEWAY_DB")"

if [ "${RUN_SIMULATOR:-true}" = "true" ]; then
  python simulator.py &
  sleep 3
fi
python gateway.py &
uvicorn web:app --host 0.0.0.0 --port "$PORT" --proxy-headers --forwarded-allow-ips="*" &

wait -n            # returns as soon as ANY of the programs stops
echo "A process exited - stopping container so Railway restarts it."
exit 1
