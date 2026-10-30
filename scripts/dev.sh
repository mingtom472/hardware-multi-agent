#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export NEXT_TELEMETRY_DISABLED=1
API_PORT="${BACKEND_PORT:-8000}"
WEB_PORT="${FRONTEND_PORT:-3000}"
export BACKEND_URL="${BACKEND_URL:-http://127.0.0.1:$API_PORT}"
[[ -x .venv/bin/python && -x frontend/node_modules/.bin/next ]] || { echo 'Run ./scripts/install.sh first.' >&2; exit 1; }
api_pid=''
web_pid=''
cleanup() {
  trap - EXIT
  for pid in "$api_pid" "$web_pid"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then kill "$pid" 2>/dev/null || true; fi
  done
  for pid in "$api_pid" "$web_pid"; do
    if [[ -n "$pid" ]]; then wait "$pid" 2>/dev/null || true; fi
  done
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
.venv/bin/python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port "$API_PORT" &
api_pid=$!
(cd frontend && exec ./node_modules/.bin/next dev --hostname 127.0.0.1 --port "$WEB_PORT") &
web_pid=$!
printf 'API port: %s; frontend port: %s; Ctrl+C stops both services.\n' "$API_PORT" "$WEB_PORT"
set +e
wait -n "$api_pid" "$web_pid"
status=$?
set -e
# A development server stopping also stops its sibling.
if [[ "$status" -eq 0 ]]; then status=1; fi
exit "$status"
