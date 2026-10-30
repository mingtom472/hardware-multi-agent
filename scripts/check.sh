#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
WITH_NEXT=1
case "${1:-}" in
  --python-only) WITH_NEXT=0; shift ;;
  --with-next) shift ;;
  "") ;;
  *) echo "Usage: $0 [--python-only|--with-next]" >&2; exit 2 ;;
esac
if [[ "$#" -ne 0 ]]; then echo "Usage: $0 [--python-only|--with-next]" >&2; exit 2; fi
export UV_CACHE_DIR="${UV_CACHE_DIR:-$ROOT/.cache/uv}"
export npm_config_cache="${npm_config_cache:-$ROOT/.cache/npm}"
export NEXT_TELEMETRY_DISABLED=1
uv run --frozen pytest backend/tests
if [[ "$WITH_NEXT" -eq 1 ]]; then
  npm --prefix frontend run typecheck
  npm --prefix frontend run build
fi
