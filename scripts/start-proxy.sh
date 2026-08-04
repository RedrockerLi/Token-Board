#!/usr/bin/env bash
# ==============================================================================
# Token Board 代理启动脚本
# Usage:
#   bash scripts/start-proxy.sh              # 前台启动（调试用）
#   bash scripts/start-proxy.sh --daemon     # 后台启动
#   bash scripts/start-proxy.sh --install    # 安装为 systemd 用户服务（开机自启）
#   bash scripts/start-proxy.sh --uninstall  # 移除 systemd 服务
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROXY_BIN="$SCRIPT_DIR/proxy/build/token_proxy"
PROXY_DB="$SCRIPT_DIR/data/proxy.db"
PROXY_PORT=8800

SERVICE_NAME="token-proxy"
SERVICE_FILE="$HOME/.config/systemd/user/${SERVICE_NAME}.service"

# ── Colors ──
RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'

# ── Build if needed ──
if [ ! -f "$PROXY_BIN" ]; then
    echo "[INFO] 代理未编译，开始编译..."
    cd "$SCRIPT_DIR/proxy"
    cmake -B build -DCMAKE_BUILD_TYPE=Release > /dev/null 2>&1
    cmake --build build -j$(nproc) > /dev/null 2>&1
    cd "$SCRIPT_DIR"
    echo "[INFO] 编译完成"
fi

# ── Functions ──

do_install() {
    echo -e "${CYAN}安装 Token Board 代理为 systemd 用户服务...${NC}"
    echo ""

    mkdir -p "$(dirname "$SERVICE_FILE")"

    cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Token Board API Proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$PROXY_BIN --db $PROXY_DB --schema-dir $SCRIPT_DIR/schema/proxy --host 127.0.0.1 --port $PROXY_PORT
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

    systemctl --user daemon-reload
    systemctl --user enable "$SERVICE_NAME"
    systemctl --user start "$SERVICE_NAME"

    echo ""
    echo -e "${GREEN}✓ 服务已安装并启动${NC}"
    echo ""
    echo "常用命令："
    echo "  systemctl --user status $SERVICE_NAME    # 查看状态"
    echo "  systemctl --user stop $SERVICE_NAME      # 停止代理"
    echo "  systemctl --user restart $SERVICE_NAME   # 重启代理"
    echo "  journalctl --user -u $SERVICE_NAME -f    # 查看日志"
    echo ""
    echo "代理地址: http://localhost:$PROXY_PORT/v1"
}

do_uninstall() {
    echo -e "${CYAN}移除 systemd 服务...${NC}"
    systemctl --user stop "$SERVICE_NAME" 2>/dev/null || true
    systemctl --user disable "$SERVICE_NAME" 2>/dev/null || true
    rm -f "$SERVICE_FILE"
    systemctl --user daemon-reload
    echo -e "${GREEN}✓ 服务已移除${NC}"
}

do_start() {
    echo -e "${CYAN}启动代理 (前台)...${NC}"
    echo "  端口: $PROXY_PORT"
    echo "  数据库: $PROXY_DB"
    echo "  按 Ctrl+C 停止"
    echo ""
    exec "$PROXY_BIN" --db "$PROXY_DB" --schema-dir "$SCRIPT_DIR/schema/proxy" --host 127.0.0.1 --port "$PROXY_PORT"
}

do_daemon() {
    # Kill existing proxy
    EXISTING=$(pgrep -f "token_proxy" 2>/dev/null || true)
    if [ -n "$EXISTING" ]; then
        echo "[INFO] 关闭已有代理 (PID: $EXISTING)..."
        kill $EXISTING 2>/dev/null || true
        sleep 1
        kill -9 $EXISTING 2>/dev/null || true
    fi

    echo -e "${CYAN}启动代理 (后台)...${NC}"
    "$PROXY_BIN" --db "$PROXY_DB" --schema-dir "$SCRIPT_DIR/schema/proxy" --port "$PROXY_PORT" &
    PROXY_PID=$!
    echo -e "${GREEN}✓ 代理已启动 (PID: $PROXY_PID)${NC}"
    echo "  代理地址: http://localhost:$PROXY_PORT/v1"
    echo "  停止: kill $PROXY_PID"
}

# ── Main ──

case "${1:-}" in
    --install)   do_install ;;
    --uninstall) do_uninstall ;;
    --daemon)    do_daemon ;;
    *)
        echo "Usage: bash scripts/start-proxy.sh [--daemon|--install|--uninstall]"
        echo ""
        echo "  (无参数)      前台启动（调试用，Ctrl+C 停止）"
        echo "  --daemon      后台启动"
        echo "  --install     安装为 systemd 用户服务，开机自启"
        echo "  --uninstall   移除 systemd 服务"
        echo ""
        # Default: foreground
        do_start
        ;;
esac
