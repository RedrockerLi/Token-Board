#!/usr/bin/env bash
# ==============================================================================
# Token Board — 一键启动
# Usage:
#   bash start.sh              仅启动仪表板
#   bash start.sh --all         启动代理（开机自启）+ 仪表板
#   bash start.sh --no-browser  不自动打开浏览器
# ==============================================================================
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROXY_BIN="${TB_PROXY_BIN:-$SCRIPT_DIR/proxy/build/token_proxy}"
DATA_DIR="${TB_DATA_DIR:-$SCRIPT_DIR/data}"
SCHEMA_DIR="${TB_SCHEMA_DIR:-$SCRIPT_DIR/schema}"
PROXY_DB="$DATA_DIR/proxy.db"
DASHBOARD_DB="$DATA_DIR/dashboard.db"
PROXY_PORT="${TB_PROXY_PORT:-8800}"
DASHBOARD_PORT="${TB_DASHBOARD_PORT:-}"
SERVICE_NAME="${TB_SERVICE_NAME:-token-proxy}"
SERVICE_FILE="${TB_SERVICE_FILE:-$HOME/.config/systemd/user/${SERVICE_NAME}.service}"
LEGACY_TIMEZONE="${TB_LEGACY_TIMEZONE:-Asia/Shanghai}"
USE_SYSTEMD=true
if [ "${TB_NO_SYSTEMD:-0}" = "1" ]; then USE_SYSTEMD=false; fi

START_ALL=false
NO_BROWSER=false
for arg in "$@"; do
    case "$arg" in
        --all) START_ALL=true ;;
        --no-browser) NO_BROWSER=true ;;
    esac
done

GREEN='\033[0;32m'; NC='\033[0m'

# Keep cleanup available before --all starts any process. A failed proxy
# health check must be able to stop the service started by this invocation.
EXIT_STATUS=0
cleanup() {
    trap - EXIT
    echo ""
    echo "[INFO] 正在关闭仪表板..."
    if [ -n "${DASHBOARD_PID:-}" ]; then
        kill "$DASHBOARD_PID" 2>/dev/null || true
        sleep 2
        kill -9 "$DASHBOARD_PID" 2>/dev/null || true
        if [ -n "${DASHBOARD_PID_FILE:-}" ]; then
            rm -f "$DASHBOARD_PID_FILE"
        fi
    fi
    if [ -n "${PROXY_PID:-}" ] && kill -0 "$PROXY_PID" 2>/dev/null; then
        kill "$PROXY_PID" 2>/dev/null || true
        if [ -n "${PROXY_PID_FILE:-}" ]; then rm -f "$PROXY_PID_FILE"; fi
    fi
    if [ "${EXIT_STATUS:-0}" != "0" ] && [ "${START_ALL:-false}" = "true" ] &&
       [ "${HAS_SYSTEMD:-false}" = "true" ]; then
        systemctl --user stop "$SERVICE_NAME" >/dev/null 2>&1 || true
    fi
    echo "[INFO] 仪表板已关闭（代理继续运行）"
    exit "${EXIT_STATUS:-0}"
}
on_exit() {
    local status=$?
    if [ "$status" -ne 0 ]; then
        EXIT_STATUS="$status"
        cleanup
    fi
}
trap on_exit EXIT
trap cleanup INT TERM

echo "============================================"
echo "  Token Board"
echo "============================================"
echo ""

# ═══════════════════════════════════════════════════════════════════════
# Proxy setup (only with --all)
# ═══════════════════════════════════════════════════════════════════════

if $START_ALL; then

    command -v cmake >/dev/null 2>&1 || { echo "[ERROR] cmake not found" >&2; exit 1; }
    command -v curl >/dev/null 2>&1 || { echo "[ERROR] curl not found" >&2; exit 1; }
    command -v python3 >/dev/null 2>&1 || { echo "[ERROR] python3 not found" >&2; exit 1; }

    # ── Build proxy  ──────────────────────────────────────────
    echo "[proxy] 编译 C++ 代理..."
    cd "$SCRIPT_DIR/proxy"
    cmake -B build -DCMAKE_BUILD_TYPE=Release > /dev/null 2>&1
    cmake --build build -j$(nproc) > /dev/null 2>&1
    cd "$SCRIPT_DIR"
    echo -e "${GREEN}✓ 编译完成${NC}"
    [ -x "$PROXY_BIN" ] || { echo "[ERROR] proxy binary not found: $PROXY_BIN" >&2; exit 1; }

    # ── Install & start via systemd (or daemon fallback) ───────────────
    HAS_SYSTEMD=false
    if $USE_SYSTEMD && systemctl --user daemon-reload 2>/dev/null; then HAS_SYSTEMD=true; fi

    # ── 先停 systemd 服务（防止 Restart=always 自动复活）──
    if $HAS_SYSTEMD && systemctl --user is-active "$SERVICE_NAME" >/dev/null 2>&1; then
        echo "[proxy] 停止 systemd 服务..."
        systemctl --user stop "$SERVICE_NAME"
    fi

    # ── 清理本项目 PID 文件对应的 fallback 进程 ──
    PROXY_PID_FILE="$DATA_DIR/token_proxy.pid"
    if [ -f "$PROXY_PID_FILE" ]; then
        EXISTING=$(cat "$PROXY_PID_FILE" || true)
        if [ -n "$EXISTING" ] && kill -0 "$EXISTING" 2>/dev/null; then
            echo "[proxy] 清理旧代理进程: $EXISTING"
            kill "$EXISTING" 2>/dev/null || true
            sleep 1
        fi
        rm -f "$PROXY_PID_FILE"
    fi

    mkdir -p "$DATA_DIR"
    echo "[schema] 检查并自动升级本地数据库..."
    PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 -m app.db.schema_upgrade.cli \
        --proxy-db "$PROXY_DB" --dashboard-db "$DASHBOARD_DB" \
        --schema-dir "$SCHEMA_DIR" --timezone "$LEGACY_TIMEZONE"
    echo -e "${GREEN}✓ 本地数据库已准备${NC}"

    if $HAS_SYSTEMD; then
        echo "[proxy] 更新 systemd 服务（开机自启）..."
        mkdir -p "$(dirname "$SERVICE_FILE")"
        SERVICE_TMP="$SERVICE_FILE.tmp.$$"
        cat > "$SERVICE_TMP" << EOF
[Unit]
Description=Token Board API Proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$PROXY_BIN --db $PROXY_DB --schema-dir $SCHEMA_DIR --host 127.0.0.1 --port $PROXY_PORT
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF
        mv -f "$SERVICE_TMP" "$SERVICE_FILE"
        systemctl --user daemon-reload
        systemctl --user enable "$SERVICE_NAME" > /dev/null 2>&1

        systemctl --user restart "$SERVICE_NAME"
        echo -e "${GREEN}✓ 代理已重启 (systemd)${NC}"
    else
        "$PROXY_BIN" --db "$PROXY_DB" --schema-dir "$SCHEMA_DIR" --host 127.0.0.1 --port "$PROXY_PORT" &
        PROXY_PID=$!
        printf '%s\n' "$PROXY_PID" > "$PROXY_PID_FILE"
        echo -e "${GREEN}✓ 代理已启动 (PID: $PROXY_PID)${NC}"
    fi
    echo "   地址: http://localhost:$PROXY_PORT/v1"
    echo "[proxy] 等待健康检查..."
    PROXY_READY=false
    for i in $(seq 1 30); do
        HEALTH_JSON=$(curl -fsS "http://127.0.0.1:$PROXY_PORT/health" 2>/dev/null || true)
        if [ -n "$HEALTH_JSON" ] && python3 -c '
import json, sys
payload = json.loads(sys.argv[1])
accounting = payload.get("accounting", {})
schema = payload.get("schema", {})
routing = payload.get("routing", {})
recovery = payload.get("recovery", {})
ok = (payload.get("status") == "ok"
      and schema.get("current") is True
      and schema.get("major") == 1
      and routing.get("loaded") is True
      and recovery.get("complete") is True
      and accounting.get("writer_healthy") is True)
raise SystemExit(0 if ok else 1)
' "$HEALTH_JSON" 2>/dev/null; then
            PROXY_READY=true
            break
        fi
        sleep 1
    done
    if ! $PROXY_READY; then
        echo "[ERROR] proxy health check failed" >&2
        EXIT_STATUS=1
        cleanup
    fi
    echo -e "${GREEN}✓ 代理健康${NC}"
    echo ""
fi

# ═══════════════════════════════════════════════════════════════════════
# Dashboard (always)
# ═══════════════════════════════════════════════════════════════════════

echo "[dash] 启动仪表板..."
if ! $START_ALL; then
    echo "   仅仪表板模式（代理不启动）"
fi
echo "   Ctrl+C 关闭"
echo ""

# Stop only the dashboard process recorded by this project.
DASHBOARD_PID_FILE="$DATA_DIR/dashboard.pid"
if [ -f "$DASHBOARD_PID_FILE" ]; then
    EXISTING_DASH=$(cat "$DASHBOARD_PID_FILE" || true)
    if [ -n "$EXISTING_DASH" ] && kill -0 "$EXISTING_DASH" 2>/dev/null; then
        kill "$EXISTING_DASH" 2>/dev/null || true
        sleep 1
    fi
    rm -f "$DASHBOARD_PID_FILE"
fi

# Ensure dependencies are already installed; startup must not mutate the
# environment or depend on network access.
if ! command -v python3 &>/dev/null; then
    echo "[ERROR] python3 not found" >&2
    EXIT_STATUS=1
    cleanup
fi
python3 -c "import flask" 2>/dev/null || {
    echo "[ERROR] Flask is not installed; install the application dependencies first" >&2
    EXIT_STATUS=1
    cleanup
}

# Select an explicit port when configured; otherwise use the first free
# project dashboard port in the historical 5000-5100 range.
if [ -n "$DASHBOARD_PORT" ]; then
    PORT="$DASHBOARD_PORT"
else
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
fi
if [ -z "$PORT" ]; then
    echo "[ERROR] no free dashboard port in the configured range" >&2
    EXIT_STATUS=1
    cleanup
fi
[ -d "$DATA_DIR" ] || mkdir -p "$DATA_DIR"

cd "$SCRIPT_DIR"
python3 server.py --port "$PORT" --proxy-db "$PROXY_DB" --host 127.0.0.1 &
DASHBOARD_PID=$!
printf '%s\n' "$DASHBOARD_PID" > "$DASHBOARD_PID_FILE"

READY=false
for i in $(seq 1 10); do
    sleep 1
    if curl -s "http://localhost:$PORT/api/summary" > /dev/null 2>&1; then
        READY=true
        break
    fi
done
if ! $READY; then
    echo "[ERROR] dashboard health check failed" >&2
    EXIT_STATUS=1
    cleanup
fi

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
