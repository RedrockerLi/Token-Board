#!/usr/bin/env bash
# ==============================================================================
# Token Board — 一键启动
#   - 首次运行：编译代理 → 安装 systemd 服务（开机自启）
#   - 后续运行：确保代理运行中 → 启动仪表板
#   - Ctrl+C 只关闭仪表板，代理持续后台运行
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROXY_BIN="$SCRIPT_DIR/proxy/build/token_proxy"
SERVICE_NAME="token-proxy"
SERVICE_FILE="$HOME/.config/systemd/user/${SERVICE_NAME}.service"
PROXY_DB="$SCRIPT_DIR/data/proxy.db"
PROXY_PORT=8800

GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'

echo "============================================"
echo "  Token Board"
echo "============================================"
echo ""

# ── Step 1: Build proxy if needed ─────────────────────────────────────
if [ ! -f "$PROXY_BIN" ]; then
    echo "[1/4] 首次运行 — 下载依赖..."
    bash "$SCRIPT_DIR/proxy/setup_deps.sh"

    echo "[2/4] 编译 C++ 代理..."
    cd "$SCRIPT_DIR/proxy"
    cmake -B build -DCMAKE_BUILD_TYPE=Release > /dev/null 2>&1
    cmake --build build -j$(nproc) > /dev/null 2>&1
    cd "$SCRIPT_DIR"
    echo -e "${GREEN}✓ 编译完成${NC}"
else
    echo "[1/4] 代理已编译，跳过"
    echo "[2/4] —"
fi

# ── Step 2: Start proxy (systemd or daemon fallback) ──────────────────

# Check if systemd --user is available
HAS_SYSTEMD=false
systemctl --user daemon-reload 2>/dev/null && HAS_SYSTEMD=true

if $HAS_SYSTEMD; then
    # ── systemd path ──────────────────────────────────────────────────
    if [ ! -f "$SERVICE_FILE" ]; then
        echo "[3/4] 安装代理为 systemd 服务（开机自启）..."

        mkdir -p "$(dirname "$SERVICE_FILE")"
        cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Token Board API Proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$PROXY_BIN --db $PROXY_DB --port $PROXY_PORT
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

        systemctl --user daemon-reload
        systemctl --user enable "$SERVICE_NAME" > /dev/null 2>&1
        echo -e "${GREEN}✓ 服务已安装（开机自启）${NC}"
    else
        echo "[3/4] systemd 服务已安装"
    fi

    if ! systemctl --user is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        systemctl --user start "$SERVICE_NAME"
        echo -e "${GREEN}✓ 代理已启动${NC}"
    else
        echo -e "${GREEN}✓ 代理运行中${NC}"
    fi
else
    # ── Daemon fallback (no systemd) ──────────────────────────────────
    echo "[3/4] systemd 不可用，后台启动代理..."
    EXISTING=$(pgrep -f "token_proxy" 2>/dev/null || true)
    if [ -n "$EXISTING" ]; then
        echo -e "${GREEN}✓ 代理已在运行 (PID: $EXISTING)${NC}"
    else
        "$PROXY_BIN" --db "$PROXY_DB" --port "$PROXY_PORT" &
        echo -e "${GREEN}✓ 代理已启动 (PID: $!)${NC}"
    fi
fi
echo "   地址: http://localhost:$PROXY_PORT/v1"
echo ""

# ── Step 3: Start dashboard (foreground) ──────────────────────────────
echo "[4/4] 启动仪表板..."
echo "   Ctrl+C 仅关闭仪表板，代理持续运行"
echo ""

# Kill old dashboard if any
EXISTING_DASH=$(pgrep -f "python3.*server\.py" 2>/dev/null || true)
if [ -n "$EXISTING_DASH" ]; then
    kill $EXISTING_DASH 2>/dev/null || true
    sleep 1
fi

# Ensure Flask is available
command -v python3 &>/dev/null || { echo "[ERROR] python3 not found"; exit 1; }
python3 -c "import flask" 2>/dev/null || pip install -q --disable-pip-version-check flask > /dev/null 2>&1

# Find a free port
PORT=$(python3 -c "
import socket
for port in range(5000, 5100):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('0.0.0.0', port))
            print(port)
            break
    except OSError:
        continue
")
[ -d "$SCRIPT_DIR/data" ] || mkdir -p "$SCRIPT_DIR/data"

# Start Python in foreground
cd "$SCRIPT_DIR"
python3 server.py --port "$PORT" --proxy-db "$PROXY_DB" &
DASHBOARD_PID=$!

# Wait for ready
for i in $(seq 1 10); do
    sleep 1
    if curl -s "http://localhost:$PORT/api/summary" > /dev/null 2>&1; then
        break
    fi
done

DASHBOARD_URL="http://localhost:$PORT"
echo ""
echo "  ➜  仪表板: $DASHBOARD_URL"
echo ""

# Open browser
if [ "${1:-}" != "--no-browser" ]; then
    if command -v xdg-open &>/dev/null; then
        xdg-open "$DASHBOARD_URL" > /dev/null 2>&1 &
    elif command -v wslview &>/dev/null; then
        wslview "$DASHBOARD_URL" > /dev/null 2>&1 &
    fi
fi

# Trap — only kills dashboard, not proxy
cleanup() {
    echo ""
    kill $DASHBOARD_PID 2>/dev/null || true
    if $HAS_SYSTEMD; then
        echo "[INFO] 仪表板已关闭，代理仍在后台运行"
        echo "  查看: systemctl --user status token-proxy"
        echo "  停止: systemctl --user stop token-proxy"
    else
        echo "[INFO] 仪表板已关闭"
    fi
    exit 0
}
trap cleanup INT TERM

wait $DASHBOARD_PID
