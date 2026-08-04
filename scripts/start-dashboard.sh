#!/usr/bin/env bash
# ==============================================================================
# Token Board 仪表板启动脚本
# Usage: bash scripts/start-dashboard.sh [--no-browser]
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OPEN_BROWSER=true
[[ "$1" == "--no-browser" ]] && OPEN_BROWSER=false

PROXY_DB="$SCRIPT_DIR/data/proxy.db"

# ── Check environment ──
command -v python3 &>/dev/null || { echo "[ERROR] python3 not found"; exit 1; }

# ── Ensure Flask is installed ──
python3 -c "import flask" 2>/dev/null || pip install -q --disable-pip-version-check flask > /dev/null 2>&1

# ── Ensure data dir exists ──
[ -d "$SCRIPT_DIR/data" ] || mkdir -p "$SCRIPT_DIR/data"

# ── Kill existing dashboard ──
EXISTING_PID=$(pgrep -f "python3.*server\.py" 2>/dev/null || true)
if [ -n "$EXISTING_PID" ]; then
    echo "[INFO] 关闭已有仪表板 (PID: $EXISTING_PID)..."
    kill $EXISTING_PID 2>/dev/null || true
    sleep 1
    kill -9 $EXISTING_PID 2>/dev/null || true
fi

# ── Find a free port ──
PORT=$(python3 -c "
import socket
for port in range(5000, 5100):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('127.0.0.1', port))
            print(port)
            break
    except OSError:
        continue
")

# ── Start dashboard ──
echo "[INFO] 启动仪表板..."
cd "$SCRIPT_DIR"
python3 server.py --port "$PORT" --proxy-db "$PROXY_DB" --host 127.0.0.1 > /dev/null 2>&1 &
SERVER_PID=$!

# ── Wait for ready ──
for i in $(seq 1 10); do
    sleep 1
    if curl -s "http://localhost:$PORT/api/summary" > /dev/null 2>&1; then
        break
    fi
done

if ! curl -s "http://localhost:$PORT/api/summary" > /dev/null 2>&1; then
    echo "[ERROR] 服务器启动失败"
    kill "$SERVER_PID" 2>/dev/null || true
    exit 1
fi

# ── Done ──
DASHBOARD_URL="http://localhost:$PORT"
echo ""
echo "  ➜  仪表板: $DASHBOARD_URL"
echo ""

# ── Open browser ──
if $OPEN_BROWSER; then
    if command -v xdg-open &>/dev/null; then
        xdg-open "$DASHBOARD_URL" > /dev/null 2>&1 &
    elif command -v wslview &>/dev/null; then
        wslview "$DASHBOARD_URL" > /dev/null 2>&1 &
    elif command -v open &>/dev/null; then
        open "$DASHBOARD_URL" > /dev/null 2>&1 &
    fi
fi

# ── Cleanup on exit ──
cleanup() {
    echo ""
    echo "[INFO] 正在关闭仪表板..."
    kill "$SERVER_PID" 2>/dev/null || true
    exit 0
}
trap cleanup INT TERM

wait $SERVER_PID
