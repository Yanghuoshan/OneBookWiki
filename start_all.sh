#!/usr/bin/env bash
# OneBookWiki unified startup script - starts both server and chat worker
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

PORT="${ONEBOOKWIKI_PORT:-8000}"
HOST="${ONEBOOKWIKI_HOST:-0.0.0.0}"
ENV_MODE="${ONEBOOKWIKI_ENV:-development}"

# ---- Command handling ----
case "${1:-start}" in
    start)
        echo "Starting OneBookWiki server and chat worker..."

        # Create logs directory first
        mkdir -p logs

        # Start chat worker in background
        echo "Starting chat worker..."
        "$PYTHON" -m server.chat_worker > logs/chat_worker.log 2>&1 &
        WORKER_PID=$!
        echo "Chat worker started (PID: $WORKER_PID)"

        # Store PIDs for stop command
        echo "$WORKER_PID" > logs/worker.pid

        # Start server (foreground)
        echo "Starting server on http://${HOST}:${PORT}..."
        if [ "$ENV_MODE" = "production" ]; then
            "$PYTHON" -m uvicorn server.main:app --host "$HOST" --port "$PORT" --workers 4 --proxy-headers --forwarded-allow-ips="*" &
            SERVER_PID=$!
            echo "$SERVER_PID" > logs/server.pid
            echo "Server started (PID: $SERVER_PID)"
            echo ""
            echo "Services running:"
            echo "  - Server: http://${HOST}:${PORT} (PID: $SERVER_PID)"
            echo "  - Chat Worker: PID $WORKER_PID"
            echo ""
            echo "Logs:"
            echo "  - Chat Worker: logs/chat_worker.log"
            echo ""
            echo "To stop: ./start_all.sh stop"
            echo "To view logs: tail -f logs/chat_worker.log"

            # Wait for both processes
            wait
        else
            "$PYTHON" -m uvicorn server.main:app --host "$HOST" --port "$PORT" --reload
        fi
        ;;

    stop)
        echo "Stopping OneBookWiki services..."

        # Stop server
        if [ -f logs/server.pid ]; then
            SERVER_PID=$(cat logs/server.pid)
            if kill -0 "$SERVER_PID" 2>/dev/null; then
                echo "Stopping server (PID: $SERVER_PID)..."
                kill "$SERVER_PID" 2>/dev/null || true
                rm logs/server.pid
            else
                echo "Server not running"
                rm logs/server.pid
            fi
        fi

        # Stop worker
        if [ -f logs/worker.pid ]; then
            WORKER_PID=$(cat logs/worker.pid)
            if kill -0 "$WORKER_PID" 2>/dev/null; then
                echo "Stopping chat worker (PID: $WORKER_PID)..."
                kill "$WORKER_PID" 2>/dev/null || true
                rm logs/worker.pid
            else
                echo "Chat worker not running"
                rm logs/worker.pid
            fi
        fi

        echo "All services stopped"
        ;;

    status)
        echo "OneBookWiki service status:"

        if [ -f logs/server.pid ]; then
            SERVER_PID=$(cat logs/server.pid)
            if kill -0 "$SERVER_PID" 2>/dev/null; then
                echo "  ✓ Server running (PID: $SERVER_PID)"
            else
                echo "  ✗ Server not running (stale PID file)"
            fi
        else
            echo "  ✗ Server not running"
        fi

        if [ -f logs/worker.pid ]; then
            WORKER_PID=$(cat logs/worker.pid)
            if kill -0 "$WORKER_PID" 2>/dev/null; then
                echo "  ✓ Chat worker running (PID: $WORKER_PID)"
            else
                echo "  ✗ Chat worker not running (stale PID file)"
            fi
        else
            echo "  ✗ Chat worker not running"
        fi
        ;;

    logs)
        if [ -f logs/chat_worker.log ]; then
            tail -f logs/chat_worker.log
        else
            echo "No chat worker log found at logs/chat_worker.log"
        fi
        ;;

    *)
        echo "Usage: $0 {start|stop|status|logs}"
        echo ""
        echo "Commands:"
        echo "  start   - Start both server and chat worker"
        echo "  stop    - Stop all services"
        echo "  status  - Check service status"
        echo "  logs    - Tail chat worker logs"
        exit 1
        ;;
esac
