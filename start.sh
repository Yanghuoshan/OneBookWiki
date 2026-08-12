#!/usr/bin/env bash
# OneBookWiki server startup script (Linux / macOS)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---- Load .env if present ----
if [ -f .env ]; then
    set -a
    # shellcheck source=/dev/null
    source .env
    set +a
    echo "[onebookwiki] Loaded environment from .env"
fi

# ---- Detect Python ----
PYTHON="${ONEBOOKWIKI_PYTHON:-python3}"
if ! command -v "$PYTHON" &>/dev/null; then
    PYTHON="python"
fi
if ! command -v "$PYTHON" &>/dev/null; then
    echo "Error: Python not found. Install Python 3.10+ and try again." >&2
    exit 1
fi

echo "Python: $($PYTHON --version)"

# ---- Start server ----
PORT="${ONEBOOKWIKI_PORT:-8000}"
HOST="${ONEBOOKWIKI_HOST:-0.0.0.0}"

echo "Starting OneBookWiki server on http://${HOST}:${PORT} ..."
exec "$PYTHON" -m uvicorn server.main:app --host "$HOST" --port "$PORT"
