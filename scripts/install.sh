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
command -v uv >/dev/null || { echo 'Install uv first: https://docs.astral.sh/uv/getting-started/installation/' >&2; exit 1; }
uv sync --frozen --python 3.12
if [[ "$WITH_NEXT" -eq 1 ]]; then
  command -v node >/dev/null || { echo 'Next.js UI requires Node.js 22 or newer.' >&2; exit 1; }
  node -e 'if (Number(process.versions.node.split(".")[0]) < 22) { console.error("Node.js 22 or newer is required"); process.exit(1); }'
  npm ci --prefix frontend
fi
printf '%s\n' 'Dependencies installed. Run ./scripts/check.sh, then ./scripts/dev.sh. Streamlit-only setup: --python-only and ./scripts/dev-studio.sh.'
