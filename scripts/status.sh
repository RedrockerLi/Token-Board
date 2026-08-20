#!/usr/bin/env bash
# ==============================================================================
# Token Board 状态检查脚本
# Usage: bash scripts/status.sh
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PROXY_SERVICE_NAME="${TB_SERVICE_NAME:-token-proxy}"
DASHBOARD_SERVICE_NAME="${TB_DASHBOARD_SERVICE_NAME:-token-dashboard}"
LEGACY_IMPORT_NAME="${TB_IMPORT_SERVICE_NAME:-token-agent-import}"
PROXY_PORT="${TB_PROXY_PORT:-8800}"
DATA_DIR="${TB_DATA_DIR:-$SCRIPT_DIR/data}"

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
PROXY_BIN="${TB_PROXY_BIN:-$SCRIPT_DIR/proxy/build/token_proxy}"
if [ -f "$PROXY_BIN" ]; then
    ok "二进制: $PROXY_BIN ($(du -h "$PROXY_BIN" | cut -f1))"
else
    fail "二进制未编译，运行 bash start.sh --all 自动构建"
fi

# ── Systemd service ──
echo ""
echo "── systemd 服务 ──"
SERVICE_FILE="$SYSTEMD_USER_DIR/${PROXY_SERVICE_NAME}.service"
if [ -f "$SERVICE_FILE" ]; then
    ok "服务文件存在"

    if systemctl --user is-active --quiet "$PROXY_SERVICE_NAME" 2>/dev/null; then
        ok "服务状态: 运行中"
    else
        warn "服务状态: 未运行 (systemctl --user start $PROXY_SERVICE_NAME)"
    fi

    if systemctl --user is-enabled --quiet "$PROXY_SERVICE_NAME" 2>/dev/null; then
        ok "开机自启: 已启用"
    else
        warn "开机自启: 未启用 (systemctl --user enable $PROXY_SERVICE_NAME)"
    fi
else
    fail "systemd 服务未安装，运行 bash start.sh --all 自动安装"
fi

# ── Dashboard service (includes usage importer) ──
echo ""
echo "── 仪表板服务 + Agent 用量导入 ──"
DASHBOARD_SERVICE_FILE="$SYSTEMD_USER_DIR/${DASHBOARD_SERVICE_NAME}.service"
if [ -f "$DASHBOARD_SERVICE_FILE" ]; then
    ok "服务文件存在"
    if systemctl --user is-active --quiet "$DASHBOARD_SERVICE_NAME" 2>/dev/null; then
        ok "服务状态: 运行中 (用量每 30 分钟由进程内 worker 导入)"
    else
        warn "服务状态: 未运行 (systemctl --user start $DASHBOARD_SERVICE_NAME)"
    fi
    if systemctl --user is-enabled --quiet "$DASHBOARD_SERVICE_NAME" 2>/dev/null; then
        ok "开机自启: 已启用"
    else
        warn "开机自启: 未启用 (systemctl --user enable $DASHBOARD_SERVICE_NAME)"
    fi
else
    fail "仪表板服务未安装，运行 bash start.sh 自动安装"
fi
if systemctl --user is-active --quiet "$LEGACY_IMPORT_NAME.timer" 2>/dev/null ||
   systemctl --user is-enabled --quiet "$LEGACY_IMPORT_NAME.timer" 2>/dev/null; then
    warn "检测到旧 $LEGACY_IMPORT_NAME.timer，重新运行 bash start.sh 完成迁移"
fi

# ── Proxy connectivity ──
echo ""
echo "── 代理连通性 ──"
if curl -s --max-time 3 "http://localhost:$PROXY_PORT/health" > /dev/null 2>&1; then
    HEALTH=$(curl -s "http://localhost:$PROXY_PORT/health")
    ok "端口 $PROXY_PORT 响应正常: $HEALTH"
else
    fail "端口 $PROXY_PORT 无响应"
fi

# ── Dashboard ──
echo ""
echo "── 仪表板 ──"
PORT="${TB_DASHBOARD_PORT:-5000}"
if curl -s --max-time 3 "http://localhost:$PORT/api/summary" > /dev/null 2>&1; then
    ok "API 响应正常: http://localhost:$PORT"
else
    warn "API 无响应 (bash start.sh)"
fi

# ── Database ──
echo ""
echo "── 数据库 ──"
DB="$DATA_DIR/proxy.db"
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
