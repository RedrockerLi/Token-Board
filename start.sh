#!/usr/bin/env bash
# Token Board launcher.
#
#   bash start.sh              quick foreground dashboard (read-only schema check)
#   bash start.sh --all        upgrade databases and restart proxy/maintenance
#   bash start.sh --no-browser suppress browser opening
set -Eeuo pipefail

SCRIPT_PATH="${BASH_SOURCE[0]}"
while [ -L "$SCRIPT_PATH" ]; do
    SCRIPT_LINK_DIR="$(cd -P "$(dirname "$SCRIPT_PATH")" && pwd)"
    SCRIPT_LINK_TARGET="$(readlink "$SCRIPT_PATH")"
    if [[ "$SCRIPT_LINK_TARGET" = /* ]]; then
        SCRIPT_PATH="$SCRIPT_LINK_TARGET"
    else
        SCRIPT_PATH="$SCRIPT_LINK_DIR/$SCRIPT_LINK_TARGET"
    fi
done
SCRIPT_DIR="$(cd -P "$(dirname "$SCRIPT_PATH")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/$(basename "$SCRIPT_PATH")"

PROXY_BIN="${TB_PROXY_BIN:-$SCRIPT_DIR/proxy/build/token_proxy}"
DATA_DIR="${TB_DATA_DIR:-$SCRIPT_DIR/data}"
SCHEMA_DIR="${TB_SCHEMA_DIR:-$SCRIPT_DIR/schema}"
TOKEN_BOARD_DB="$DATA_DIR/token-board.db"
DASHBOARD_DB="$DATA_DIR/dashboard.db"
PROXY_PORT="${TB_PROXY_PORT:-8800}"
DASHBOARD_PORT="${TB_DASHBOARD_PORT:-5000}"
DASHBOARD_PID_FILE="$DATA_DIR/dashboard.pid"
PROXY_PID_FILE="$DATA_DIR/token_proxy.pid"
MAINTENANCE_PID_FILE="$DATA_DIR/token-maintenance.pid"
MAINTENANCE_SOCKET="$DATA_DIR/token-maintenance.sock"
MAINTENANCE_HEALTH="$DATA_DIR/token-maintenance-health.json"
SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PROXY_SERVICE_NAME="${TB_SERVICE_NAME:-token-proxy}"
PROXY_SERVICE_FILE="${TB_SERVICE_FILE:-$SYSTEMD_USER_DIR/${PROXY_SERVICE_NAME}.service}"
MAINTENANCE_SERVICE_NAME="${TB_MAINTENANCE_SERVICE_NAME:-token-maintenance}"
MAINTENANCE_SERVICE_FILE="${TB_MAINTENANCE_SERVICE_FILE:-$SYSTEMD_USER_DIR/${MAINTENANCE_SERVICE_NAME}.service}"
LEGACY_IMPORT_NAME="${TB_IMPORT_SERVICE_NAME:-token-agent-import}"
LEGACY_IMPORT_SERVICE_FILE="${TB_IMPORT_SERVICE_FILE:-$SYSTEMD_USER_DIR/${LEGACY_IMPORT_NAME}.service}"
LEGACY_IMPORT_TIMER_FILE="${TB_IMPORT_TIMER_FILE:-$SYSTEMD_USER_DIR/${LEGACY_IMPORT_NAME}.timer}"
LEGACY_TIMEZONE="${TB_LEGACY_TIMEZONE:-Asia/Shanghai}"

START_ALL=false
NO_BROWSER=false
for arg in "$@"; do
    case "$arg" in
        --all) START_ALL=true ;;
        --no-browser) NO_BROWSER=true ;;
        *) echo "[ERROR] unknown option: $arg" >&2; exit 2 ;;
    esac
done

GREEN='\033[0;32m'; YELLOW='\033[0;33m'; NC='\033[0m'

select_python() {
    local candidate
    if [ -n "${TB_PYTHON_BIN:-}" ]; then
        candidate="$TB_PYTHON_BIN"
        if [[ "$candidate" != /* ]] || [ ! -x "$candidate" ] ||
           ! "$candidate" -c 'import flask, requests' >/dev/null 2>&1; then
            echo "[ERROR] TB_PYTHON_BIN must be an absolute Python path with flask and requests: $candidate" >&2
            return 1
        fi
        printf '%s\n' "$candidate"
        return
    fi
    for candidate in "$(command -v python3 2>/dev/null || true)" \
                     "$HOME/miniconda3/bin/python3" \
                     "$HOME/anaconda3/bin/python3"; do
        [ -n "$candidate" ] || continue
        if [[ "$candidate" != /* ]]; then
            candidate="$(cd -P "$(dirname "$candidate")" && pwd)/$(basename "$candidate")"
        fi
        if [ -x "$candidate" ] &&
           "$candidate" -c 'import flask, requests' >/dev/null 2>&1; then
            printf '%s\n' "$candidate"
            return
        fi
    done
    echo "[ERROR] no Python interpreter with flask and requests was found" >&2
    echo "        Set TB_PYTHON_BIN=/absolute/path/to/python3 and retry." >&2
    return 1
}

command -v curl >/dev/null 2>&1 || { echo "[ERROR] curl not found" >&2; exit 1; }
PYTHON_BIN="$(select_python)"
mkdir -p "$DATA_DIR"

USE_SYSTEMD=true
[ "${TB_NO_SYSTEMD:-0}" = "1" ] && USE_SYSTEMD=false
HAS_SYSTEMD=false
if $USE_SYSTEMD && command -v systemctl >/dev/null 2>&1 &&
   systemctl --user daemon-reload 2>/dev/null; then
    HAS_SYSTEMD=true
fi

EXIT_STATUS=0
DASHBOARD_PID=""
PROXY_PID=""
MAINTENANCE_PID=""
NEW_SERVICES_STARTED=false
SERVICES_STOPPED_FOR_UPGRADE=false

process_command() {
    local pid="$1"
    if [ -r "/proc/$pid/cmdline" ]; then
        tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true
    else
        ps -p "$pid" -o command= 2>/dev/null || true
    fi
}

is_managed_dashboard_pid() {
    local pid="$1" command
    command="$(process_command "$pid")"
    [[ "$command" == *"$SCRIPT_DIR/server.py"* ]]
}

stop_managed_pid() {
    local pid="$1" label="$2"
    kill "$pid" 2>/dev/null || true
    for ((i = 1; i <= 15; i++)); do
        if ! kill -0 "$pid" 2>/dev/null; then return 0; fi
        sleep 1
    done
    echo "[$label] 进程未在 15 秒内退出，强制停止 (PID: $pid)" >&2
    kill -9 "$pid" 2>/dev/null || true
    ! kill -0 "$pid" 2>/dev/null
}

stop_legacy_import_units() {
    if ! $HAS_SYSTEMD; then return 0; fi
    systemctl --user disable --now "$LEGACY_IMPORT_NAME.timer" >/dev/null 2>&1 || true
    systemctl --user stop "$LEGACY_IMPORT_NAME.service" >/dev/null 2>&1 || true
    rm -f "$LEGACY_IMPORT_SERVICE_FILE" "$LEGACY_IMPORT_TIMER_FILE"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
}

write_proxy_service_unit() {
    mkdir -p "$(dirname "$PROXY_SERVICE_FILE")"
    cat > "$PROXY_SERVICE_FILE" <<EOF
[Unit]
Description=Token Board API Proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
Environment="PYTHONPATH=$SCRIPT_DIR"
ExecStart="$PROXY_BIN" --db "$TOKEN_BOARD_DB" --schema-dir "$SCHEMA_DIR" --host 127.0.0.1 --port $PROXY_PORT
Restart=always
RestartSec=5
TimeoutStopSec=15
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF
}

write_maintenance_service_unit() {
    mkdir -p "$(dirname "$MAINTENANCE_SERVICE_FILE")"
    cat > "$MAINTENANCE_SERVICE_FILE" <<EOF
[Unit]
Description=Token Board Runtime Maintenance
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
Environment="PYTHONPATH=$SCRIPT_DIR"
UMask=0077
ExecStart="$PYTHON_BIN" "$SCRIPT_DIR/maintenance.py" --token-board-db "$TOKEN_BOARD_DB" --schema-dir "$SCHEMA_DIR" --socket "$MAINTENANCE_SOCKET" --health "$MAINTENANCE_HEALTH"
Restart=on-failure
RestartSec=5
TimeoutStopSec=15
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF
}

cleanup() {
    trap - EXIT INT TERM
    if [ -n "$DASHBOARD_PID" ] && kill -0 "$DASHBOARD_PID" 2>/dev/null; then
        echo ""
        echo "[INFO] 正在关闭仪表板..."
        kill "$DASHBOARD_PID" 2>/dev/null || true
        sleep 1
        kill -0 "$DASHBOARD_PID" 2>/dev/null && kill -9 "$DASHBOARD_PID" 2>/dev/null || true
    fi
    rm -f "$DASHBOARD_PID_FILE"
    if [ "$EXIT_STATUS" != "0" ] && [ "$NEW_SERVICES_STARTED" = true ] && ! $HAS_SYSTEMD; then
        [ -z "$MAINTENANCE_PID" ] || kill "$MAINTENANCE_PID" 2>/dev/null || true
        [ -z "$PROXY_PID" ] || kill "$PROXY_PID" 2>/dev/null || true
        rm -f "$MAINTENANCE_PID_FILE" "$PROXY_PID_FILE"
    fi
    if [ "$EXIT_STATUS" != "0" ] && [ "$SERVICES_STOPPED_FOR_UPGRADE" = true ] && $HAS_SYSTEMD &&
       [ "$NEW_SERVICES_STARTED" = false ]; then
        systemctl --user start "$PROXY_SERVICE_NAME" "$MAINTENANCE_SERVICE_NAME" >/dev/null 2>&1 || true
    fi
    exit "$EXIT_STATUS"
}

on_exit() {
    local status=$?
    if [ "$status" -ne 0 ]; then EXIT_STATUS="$status"; cleanup; fi
}
trap on_exit EXIT
trap 'EXIT_STATUS=130; cleanup' INT TERM

open_browser() {
    local url="$1"
    $NO_BROWSER && return
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$url" >/dev/null 2>&1 &
    elif command -v wslview >/dev/null 2>&1; then
        wslview "$url" >/dev/null 2>&1 &
    elif command -v open >/dev/null 2>&1; then
        open "$url" >/dev/null 2>&1 &
    fi
}

wait_for_url() {
    local url="$1" attempts="$2"
    for ((i = 1; i <= attempts; i++)); do
        if curl -fsS "$url" >/dev/null 2>&1; then return 0; fi
        sleep 1
    done
    return 1
}

wait_for_proxy() {
    for ((i = 1; i <= 30; i++)); do
        local payload
        payload="$(curl -fsS "http://127.0.0.1:$PROXY_PORT/health" 2>/dev/null || true)"
        if [ -n "$payload" ] && "$PYTHON_BIN" -c '
import json, sys
p=json.loads(sys.argv[1]); s=p.get("schema", {}); r=p.get("routing", {})
raise SystemExit(0 if p.get("status")=="ok" and s.get("current") is True and r.get("loaded") is True else 1)
' "$payload" 2>/dev/null; then return 0; fi
        sleep 1
    done
    return 1
}

wait_for_maintenance() {
    for ((i = 1; i <= 30; i++)); do
        if [ -S "$MAINTENANCE_SOCKET" ] && [ -f "$MAINTENANCE_HEALTH" ] &&
           "$PYTHON_BIN" -c '
import json,sys
p=json.load(open(sys.argv[1])); raise SystemExit(0 if p.get("heartbeat_at") and p.get("tasks") else 1)
' "$MAINTENANCE_HEALTH" 2>/dev/null; then return 0; fi
        sleep 1
    done
    return 1
}

databases_are_current() {
    PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" - \
        "$TOKEN_BOARD_DB" "$DASHBOARD_DB" "$SCHEMA_DIR" <<'PY'
from pathlib import Path
import json
import sys

from app.db.schema_upgrade import verify_current_database

proxy = Path(sys.argv[1])
dashboard = Path(sys.argv[2])
schema = sys.argv[3]
try:
    verify_current_database(proxy, "token-board", schema)
    verify_current_database(dashboard, "dashboard", schema)
    snapshot = proxy.parent / "token-board_config_snapshot.db"
    if snapshot.exists():
        verify_current_database(snapshot, "token-board", schema)
    for manifest in proxy.parent.glob("auto-*.manifest.json"):
        try:
            stage = json.loads(manifest.read_text(encoding="utf-8")).get("stage")
        except Exception:
            raise SystemExit(1)
        if stage not in {"complete", "recovered_rollback"}:
            raise SystemExit(1)
except Exception:
    raise SystemExit(1)
raise SystemExit(0)
PY
}

launch_fallback_proxy() {
    "$PROXY_BIN" --db "$TOKEN_BOARD_DB" --schema-dir "$SCHEMA_DIR" \
        --host 127.0.0.1 --port "$PROXY_PORT" >/dev/null 2>&1 &
    PROXY_PID=$!
    printf '%s\n' "$PROXY_PID" > "$PROXY_PID_FILE"
}

launch_fallback_maintenance() {
    PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" \
        "$SCRIPT_DIR/maintenance.py" --token-board-db "$TOKEN_BOARD_DB" \
        --schema-dir "$SCHEMA_DIR" --socket "$MAINTENANCE_SOCKET" \
        --health "$MAINTENANCE_HEALTH" >/dev/null 2>&1 &
    MAINTENANCE_PID=$!
    printf '%s\n' "$MAINTENANCE_PID" > "$MAINTENANCE_PID_FILE"
}

echo "============================================"
echo "  Token Board"
echo "============================================"
echo ""

if $START_ALL; then
    command -v cmake >/dev/null 2>&1 || { echo "[ERROR] cmake not found" >&2; exit 1; }
    echo "[proxy] 编译 C++ 代理..."
    cmake -S "$SCRIPT_DIR/proxy" -B "$SCRIPT_DIR/proxy/build" \
        -DCMAKE_BUILD_TYPE=Release >/dev/null 2>&1
    BUILD_JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"
    cmake --build "$SCRIPT_DIR/proxy/build" -j"$BUILD_JOBS" >/dev/null 2>&1
    [ -x "$PROXY_BIN" ] || { echo "[ERROR] proxy binary not found: $PROXY_BIN" >&2; exit 1; }
    echo -e "${GREEN}✓ 编译完成${NC}"

    if databases_are_current; then
        echo "[schema] 本地数据库已是最新版，跳过完整升级"
    else
        if $HAS_SYSTEMD; then
            # Apply a bounded stop policy before waiting for a potentially
            # hung upstream request. The generated unit below persists it.
            echo "[services] 停止 token-proxy 与 token-maintenance（最多等待 15 秒）..."
            write_proxy_service_unit
            if [ -f "$MAINTENANCE_SERVICE_FILE" ]; then
                write_maintenance_service_unit
            fi
            systemctl --user daemon-reload >/dev/null 2>&1 || true
            systemctl --user stop "$PROXY_SERVICE_NAME" "$MAINTENANCE_SERVICE_NAME" >/dev/null 2>&1 || true
            SERVICES_STOPPED_FOR_UPGRADE=true
            stop_legacy_import_units
            rm -f "$MAINTENANCE_HEALTH"
        else
            [ -f "$PROXY_PID_FILE" ] && rm -f "$PROXY_PID_FILE"
            [ -f "$MAINTENANCE_PID_FILE" ] && rm -f "$MAINTENANCE_PID_FILE"
            rm -f "$MAINTENANCE_HEALTH"
        fi

        echo "[schema] 检查并升级本地数据库..."
        PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" \
            -m app.db.schema_upgrade.cli \
            --token-board-db "$TOKEN_BOARD_DB" --dashboard-db "$DASHBOARD_DB" \
            --schema-dir "$SCHEMA_DIR" --timezone "$LEGACY_TIMEZONE"
        echo -e "${GREEN}✓ 本地数据库已准备${NC}"
    fi

    if $HAS_SYSTEMD; then
        stop_legacy_import_units
        rm -f "$MAINTENANCE_HEALTH"
        write_proxy_service_unit
        write_maintenance_service_unit
        systemctl --user daemon-reload
        systemctl --user enable "$PROXY_SERVICE_NAME" "$MAINTENANCE_SERVICE_NAME" >/dev/null 2>&1
        SERVICES_STOPPED_FOR_UPGRADE=true
        echo "[services] 重启 token-proxy 与 token-maintenance..."
        systemctl --user restart "$PROXY_SERVICE_NAME" "$MAINTENANCE_SERVICE_NAME"
    else
        launch_fallback_proxy
        launch_fallback_maintenance
    fi
    NEW_SERVICES_STARTED=true

    wait_for_proxy || { echo "[ERROR] proxy health check failed" >&2; exit 1; }
    wait_for_maintenance || { echo "[ERROR] maintenance health check failed" >&2; exit 1; }
    echo -e "${GREEN}✓ 后台服务健康${NC}"
else
    echo "[dash] 快速启动模式：不迁移数据库、不重启后台服务"
fi

if [ -f "$DASHBOARD_PID_FILE" ]; then
    EXISTING_DASHBOARD="$(sed -n '1p' "$DASHBOARD_PID_FILE" 2>/dev/null || true)"
    if [[ "$EXISTING_DASHBOARD" =~ ^[0-9]+$ ]] &&
       kill -0 "$EXISTING_DASHBOARD" 2>/dev/null; then
        if ! is_managed_dashboard_pid "$EXISTING_DASHBOARD"; then
            echo "[ERROR] dashboard.pid 指向非本项目进程，拒绝停止: $EXISTING_DASHBOARD" >&2
            exit 1
        fi
        stop_managed_pid "$EXISTING_DASHBOARD" dashboard
    fi
    rm -f "$DASHBOARD_PID_FILE"
fi

echo "[dash] 启动前台仪表板（Ctrl+C 关闭）..."
cd "$SCRIPT_DIR"
PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" \
    "$SCRIPT_DIR/server.py" --port "$DASHBOARD_PORT" \
    --token-board-db "$TOKEN_BOARD_DB" --schema-dir "$SCHEMA_DIR" \
    --host 127.0.0.1 >/dev/null 2>&1 &
DASHBOARD_PID=$!
printf '%s\n' "$DASHBOARD_PID" > "$DASHBOARD_PID_FILE"

if ! wait_for_url "http://127.0.0.1:$DASHBOARD_PORT/api/summary" 30; then
    echo "[ERROR] dashboard health check failed" >&2
    EXIT_STATUS=1
    cleanup
fi

echo ""
echo "  ➜  仪表板: http://localhost:$DASHBOARD_PORT"
echo "  ➜  配置: 启动后异步拉取云端，完成前只读"
echo ""
open_browser "http://localhost:$DASHBOARD_PORT"
STARTUP_COMMITTED=true
wait "$DASHBOARD_PID"
