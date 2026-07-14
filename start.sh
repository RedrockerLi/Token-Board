#!/usr/bin/env bash
# ==============================================================================
# Token Board — 一键启动
# Usage:
#   bash start.sh              仅启动仪表板
#   bash start.sh --all         启动代理（开机自启）+ 仪表板
#   bash start.sh --no-browser  不自动打开浏览器
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROXY_BIN="$SCRIPT_DIR/proxy/build/token_proxy"
SERVICE_NAME="token-proxy"
SERVICE_FILE="$HOME/.config/systemd/user/${SERVICE_NAME}.service"
PROXY_DB="$SCRIPT_DIR/data/proxy.db"
PROXY_PORT=8800

START_ALL=false
NO_BROWSER=false
for arg in "$@"; do
    case "$arg" in
        --all) START_ALL=true ;;
        --no-browser) NO_BROWSER=true ;;
    esac
done

GREEN='\033[0;32m'; NC='\033[0m'

echo "============================================"
echo "  Token Board"
echo "============================================"
echo ""

# ═══════════════════════════════════════════════════════════════════════
# Proxy setup (only with --all)
# ═══════════════════════════════════════════════════════════════════════

if $START_ALL; then

    # ── Build proxy if needed ──────────────────────────────────────────
    if [ ! -f "$PROXY_BIN" ]; then
        echo "[proxy] 编译 C++ 代理..."
        cd "$SCRIPT_DIR/proxy"
        cmake -B build -DCMAKE_BUILD_TYPE=Release > /dev/null 2>&1
        cmake --build build -j$(nproc) > /dev/null 2>&1
        cd "$SCRIPT_DIR"
        echo -e "${GREEN}✓ 编译完成${NC}"
    fi

    # ── Install & start via systemd (or daemon fallback) ───────────────
    HAS_SYSTEMD=false
    systemctl --user daemon-reload 2>/dev/null && HAS_SYSTEMD=true

    # ── 先停 systemd 服务（防止 Restart=always 自动复活）──
    if $HAS_SYSTEMD && systemctl --user is-active "$SERVICE_NAME" >/dev/null 2>&1; then
        echo "[proxy] 停止 systemd 服务..."
        systemctl --user stop "$SERVICE_NAME"
    fi

    # ── 清理所有残留代理进程（包括非 systemd 启动的）──
    EXISTING=$(pgrep -f "token_proxy" 2>/dev/null || true)
    if [ -n "$EXISTING" ]; then
        echo "[proxy] 清理旧代理进程: $EXISTING"
        kill $EXISTING 2>/dev/null || true
        sleep 1
        STILL=$(pgrep -f "token_proxy" 2>/dev/null || true)
        if [ -n "$STILL" ]; then
            echo "[proxy] SIGTERM 无效，使用 SIGKILL: $STILL"
            kill -9 $STILL 2>/dev/null || true
            sleep 1
        fi
    fi

    if $HAS_SYSTEMD; then
        if [ ! -f "$SERVICE_FILE" ]; then
            echo "[proxy] 安装 systemd 服务（开机自启）..."
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
        fi

        systemctl --user restart "$SERVICE_NAME"
        echo -e "${GREEN}✓ 代理已重启 (systemd)${NC}"
    else
        "$PROXY_BIN" --db "$PROXY_DB" --port "$PROXY_PORT" &
        PROXY_PID=$!
        echo -e "${GREEN}✓ 代理已启动 (PID: $PROXY_PID)${NC}"
    fi
    echo "   地址: http://localhost:$PROXY_PORT/v1"
    echo ""
fi

# ═══════════════════════════════════════════════════════════════════════
# Dashboard (always)
# ═══════════════════════════════════════════════════════════════════════

# ── cleanup: 必须在启动后台进程之前注册 ──────────────────────────
#    这样即使在启动等待循环中按 Ctrl+C 也能正确清理
cleanup() {
    echo ""
    echo "[INFO] 正在关闭仪表板..."
    # 先尝试优雅关闭 (SIGTERM)
    if [ -n "${DASHBOARD_PID:-}" ]; then
        kill $DASHBOARD_PID 2>/dev/null || true
        sleep 2
        kill -9 $DASHBOARD_PID 2>/dev/null || true
    fi
    echo "[INFO] 仪表板已关闭（代理继续运行）"
    exit 0
}
trap cleanup INT TERM

echo "[dash] 启动仪表板..."
if ! $START_ALL; then
    echo "   仅仪表板模式（代理不启动）"
fi
echo "   Ctrl+C 关闭"
echo ""

# Kill old dashboard
EXISTING_DASH=$(pgrep -f "python3.*server\.py" 2>/dev/null || true)
if [ -n "$EXISTING_DASH" ]; then
    kill $EXISTING_DASH 2>/dev/null || true
    sleep 1
fi

# Ensure deps
command -v python3 &>/dev/null || { echo "[ERROR] python3 not found"; exit 1; }
python3 -c "import flask" 2>/dev/null || pip install -q --disable-pip-version-check flask > /dev/null 2>&1

# Import CSV files
echo "[dash] 导入 CSV 文件..."
cd "$SCRIPT_DIR"
python3 -m app.import_csv --data-dir data --db "$SCRIPT_DIR/data/dashboard.db" 2>&1 || true

# Find free port
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

cd "$SCRIPT_DIR"
python3 server.py --port "$PORT" --proxy-db "$PROXY_DB" &
DASHBOARD_PID=$!

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

if ! $NO_BROWSER; then
    if command -v xdg-open &>/dev/null; then
        xdg-open "$DASHBOARD_URL" > /dev/null 2>&1 &
    elif command -v wslview &>/dev/null; then
        wslview "$DASHBOARD_URL" > /dev/null 2>&1 &
    fi
fi

wait $DASHBOARD_PID
