#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
API_PORT="${BACKEND_PORT:-8000}"
UI_PORT="${STREAMLIT_PORT:-8501}"
export HW_API_URL="${HW_API_URL:-http://127.0.0.1:$API_PORT}"
export HW_BROWSER_API_URL="${HW_BROWSER_API_URL:-http://127.0.0.1:$API_PORT}"
export HW_CORS_ORIGINS="${HW_CORS_ORIGINS:-[\"http://127.0.0.1:$UI_PORT\",\"http://localhost:$UI_PORT\",\"http://127.0.0.1:3000\",\"http://localhost:3000\"]}"
[[ -x .venv/bin/python ]] || { echo 'Run ./scripts/install.sh first.' >&2; exit 1; }
api_pid=''
ui_pid=''
cleanup() {
  trap - EXIT
  for pid in "$api_pid" "$ui_pid"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then kill "$pid" 2>/dev/null || true; fi
  done
  for pid in "$api_pid" "$ui_pid"; do
    if [[ -n "$pid" ]]; then wait "$pid" 2>/dev/null || true; fi
  done
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
.venv/bin/python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port "$API_PORT" &
api_pid=$!
.venv/bin/python -m streamlit run ui/streamlit_app.py --server.address 127.0.0.1 --server.port "$UI_PORT" --browser.gatherUsageStats false &
ui_pid=$!
printf 'API port: %s; Engineering AI Studio port: %s; Ctrl+C stops both services.\n' "$API_PORT" "$UI_PORT"
set +e
wait -n "$api_pid" "$ui_pid"
status=$?
set -e
if [[ "$status" -eq 0 ]]; then status=1; fi
exit "$status"
