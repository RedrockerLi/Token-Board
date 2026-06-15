#!/usr/bin/env bash
# ==============================================================================
# Token Board 状态检查脚本
# Usage: bash scripts/status.sh
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; NC='\033[0m'

ok()  { echo -e "  ${GREEN}✓${NC} $1"; }
fail(){ echo -e "  ${RED}✗${NC} $1"; }
warn(){ echo -e "  ${YELLOW}⚠${NC} $1"; }

echo "============================================"
echo "  Token Board 状态检查"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================"
echo ""

# ── Proxy binary ──
echo "── 代理程序 ──"
PROXY_BIN="$SCRIPT_DIR/proxy/build/token_proxy"
if [ -f "$PROXY_BIN" ]; then
    ok "二进制: $PROXY_BIN ($(du -h "$PROXY_BIN" | cut -f1))"
else
    fail "二进制未编译，运行 bash start.sh 自动构建"
fi

# ── Systemd service ──
echo ""
echo "── systemd 服务 ──"
SERVICE_FILE="$HOME/.config/systemd/user/token-proxy.service"
if [ -f "$SERVICE_FILE" ]; then
    ok "服务文件存在"

    if systemctl --user is-active --quiet token-proxy 2>/dev/null; then
        ok "服务状态: 运行中"
    else
        warn "服务状态: 未运行 (systemctl --user start token-proxy)"
    fi

    if systemctl --user is-enabled --quiet token-proxy 2>/dev/null; then
        ok "开机自启: 已启用"
    else
        warn "开机自启: 未启用 (systemctl --user enable token-proxy)"
    fi
else
    fail "systemd 服务未安装，运行 bash start.sh 自动安装"
fi

# ── Proxy connectivity ──
echo ""
echo "── 代理连通性 ──"
if curl -s --max-time 3 http://localhost:8800/health > /dev/null 2>&1; then
    HEALTH=$(curl -s http://localhost:8800/health)
    ok "端口 8800 响应正常: $HEALTH"
else
    fail "端口 8800 无响应"
fi

# ── Dashboard ──
echo ""
echo "── 仪表板 ──"
DASHBOARD_PID=$(pgrep -f "python3.*server\.py" 2>/dev/null || true)
if [ -n "$DASHBOARD_PID" ]; then
    PORT=$(ss -tlnp 2>/dev/null | grep "$DASHBOARD_PID" | awk '{print $4}' | grep -oP ':\K\d+' | head -1)
    ok "进程运行中 (PID: $DASHBOARD_PID, 端口: ${PORT:-未知})"
    if [ -n "$PORT" ] && curl -s --max-time 3 "http://localhost:$PORT/api/summary" > /dev/null 2>&1; then
        ok "API 响应正常"
    else
        warn "API 无响应"
    fi
else
    warn "仪表板未运行 (bash scripts/start-dashboard.sh)"
fi

# ── Database ──
echo ""
echo "── 数据库 ──"
DB="$SCRIPT_DIR/data/proxy.db"
if [ -f "$DB" ]; then
    SIZE=$(du -h "$DB" | cut -f1)
    REQUESTS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM request_log" 2>/dev/null || echo "?")
    ACCOUNTS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM upstream_accounts" 2>/dev/null || echo "?")
    KEYS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM local_keys" 2>/dev/null || echo "?")
    ok "数据库: $SIZE, 请求: $REQUESTS 条, 账户: $ACCOUNTS, 密钥: $KEYS"
else
    warn "数据库未创建（首次请求后自动生成）"
fi

echo ""
