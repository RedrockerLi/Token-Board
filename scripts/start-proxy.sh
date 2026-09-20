#!/usr/bin/env bash
# ==============================================================================
# Token Board 代理启动脚本
# Usage:
#   bash scripts/start-proxy.sh              # 前台启动（调试用）
#   bash scripts/start-proxy.sh --debug      # 暂停 systemd，前台详细调试
#   bash scripts/start-proxy.sh --daemon     # 后台启动
#   bash scripts/start-proxy.sh --install    # 安装为 systemd 用户服务（开机自启）
#   bash scripts/start-proxy.sh --uninstall  # 移除 systemd 服务
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROXY_BIN="$SCRIPT_DIR/proxy/build/token_proxy"
TOKEN_BOARD_DB="$SCRIPT_DIR/data/token-board.db"
SCHEMA_DIR="${TB_SCHEMA_DIR:-$SCRIPT_DIR/schema}"
DASHBOARD_DB="$SCRIPT_DIR/data/dashboard.db"
PROXY_PORT=8800
PYTHON_BIN="${TB_PYTHON_BIN:-python3}"

SERVICE_NAME="token-proxy"
SERVICE_FILE="$HOME/.config/systemd/user/${SERVICE_NAME}.service"
DEBUG_SERVICE_WAS_ACTIVE=false

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

verify_schema() {
    PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
        "$PYTHON_BIN" -c '
from app.db.schema_upgrade import verify_current_database
import sys
verify_current_database(sys.argv[1], "token-board", sys.argv[2])
' "$TOKEN_BOARD_DB" "$SCHEMA_DIR" || {
        echo "[ERROR] 数据库未处于当前 schema，请运行 bash start.sh --all" >&2
        return 1
    }
}

do_install() {
    echo -e "${CYAN}安装 Token Board 代理为 systemd 用户服务...${NC}"
    echo ""

    verify_schema

    mkdir -p "$(dirname "$SERVICE_FILE")"

    cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Token Board API Proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
Environment=PYTHONPATH=$SCRIPT_DIR
ExecStart=$PROXY_BIN --db $TOKEN_BOARD_DB --schema-dir $SCHEMA_DIR --host 127.0.0.1 --port $PROXY_PORT
Restart=always
RestartSec=5
# Keep info/debug out of the persistent journal; warnings/errors remain there.
StandardOutput=null
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
    echo "  journalctl --user -u $SERVICE_NAME -f    # 查看 warning/error"
    echo "  bash scripts/start-proxy.sh --debug     # 终端查看完整 debug"
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
    verify_schema
    echo -e "${CYAN}启动代理 (前台)...${NC}"
    echo "  端口: $PROXY_PORT"
    echo "  数据库: $TOKEN_BOARD_DB"
    echo "  按 Ctrl+C 停止"
    echo ""
    exec "$PROXY_BIN" --db "$TOKEN_BOARD_DB" --schema-dir "$SCHEMA_DIR" --host 127.0.0.1 --port "$PROXY_PORT"
}

do_debug() {
    verify_schema

    cleanup_debug() {
        local status=$?
        trap - EXIT INT TERM
        if $DEBUG_SERVICE_WAS_ACTIVE; then
            echo -e "${CYAN}恢复 systemd 服务 $SERVICE_NAME...${NC}"
            if ! systemctl --user start "$SERVICE_NAME"; then
                echo -e "${RED}[ERROR] 无法恢复 systemd 服务 $SERVICE_NAME${NC}" >&2
                [ "$status" -eq 0 ] && status=1
            fi
        fi
        exit "$status"
    }
    trap cleanup_debug EXIT
    trap 'exit 130' INT TERM

    if command -v systemctl >/dev/null 2>&1 &&
       systemctl --user is-active --quiet "$SERVICE_NAME"; then
        DEBUG_SERVICE_WAS_ACTIVE=true
        echo -e "${CYAN}暂停 systemd 服务 $SERVICE_NAME，进入前台 debug...${NC}"
        systemctl --user stop "$SERVICE_NAME"
    fi

    echo "详细日志直接输出到当前终端；按 Ctrl+C 退出并恢复原服务状态。"
    "$PROXY_BIN" --db "$TOKEN_BOARD_DB" --schema-dir "$SCHEMA_DIR" \
        --host 127.0.0.1 --port "$PROXY_PORT" --log-level debug
}

do_daemon() {
    verify_schema
    # Kill existing proxy
    EXISTING=$(pgrep -f "token_proxy" 2>/dev/null || true)
    if [ -n "$EXISTING" ]; then
        echo "[INFO] 关闭已有代理 (PID: $EXISTING)..."
        kill $EXISTING 2>/dev/null || true
        sleep 1
        kill -9 $EXISTING 2>/dev/null || true
    fi

    echo -e "${CYAN}启动代理 (后台)...${NC}"
    "$PROXY_BIN" --db "$TOKEN_BOARD_DB" --schema-dir "$SCHEMA_DIR" --port "$PROXY_PORT" &
    PROXY_PID=$!
    echo -e "${GREEN}✓ 代理已启动 (PID: $PROXY_PID)${NC}"
    echo "  代理地址: http://localhost:$PROXY_PORT/v1"
    echo "  停止: kill $PROXY_PID"
}

# ── Main ──

case "${1:-}" in
    --install)   do_install ;;
    --uninstall) do_uninstall ;;
    --debug)     do_debug ;;
    --daemon)    do_daemon ;;
    *)
        echo "Usage: bash scripts/start-proxy.sh [--debug|--daemon|--install|--uninstall]"
        echo ""
        echo "  (无参数)      前台启动（调试用，Ctrl+C 停止）"
        echo "  --debug       暂停 systemd，前台输出完整 debug 日志"
        echo "  --daemon      后台启动"
        echo "  --install     安装为 systemd 用户服务，开机自启"
        echo "  --uninstall   移除 systemd 服务"
        echo ""
        # Default: foreground
        do_start
        ;;
esac
