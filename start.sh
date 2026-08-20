#!/usr/bin/env bash
# ==============================================================================
# Token Board — 一键启动
# Usage:
#   bash start.sh              启动并设置仪表板开机自启
#   bash start.sh --all        额外编译并设置 API 代理开机自启
#   bash start.sh --no-browser 不自动打开浏览器
#
# Environment overrides:
#   TB_PYTHON_BIN, TB_DATA_DIR, TB_DASHBOARD_PORT, TB_NO_SYSTEMD, ...
# ==============================================================================
set -Eeuo pipefail

# Resolve symlinks without GNU-only ``readlink -f`` so the foreground fallback
# remains usable on non-systemd platforms such as macOS.
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
PROXY_DB="$DATA_DIR/proxy.db"
DASHBOARD_DB="$DATA_DIR/dashboard.db"
PROXY_PORT="${TB_PROXY_PORT:-8800}"
DASHBOARD_PORT="${TB_DASHBOARD_PORT:-5000}"
DASHBOARD_PID_FILE="$DATA_DIR/dashboard.pid"
PROXY_PID_FILE="$DATA_DIR/token_proxy.pid"
SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PROXY_SERVICE_NAME="${TB_SERVICE_NAME:-token-proxy}"
PROXY_SERVICE_FILE="${TB_SERVICE_FILE:-$SYSTEMD_USER_DIR/${PROXY_SERVICE_NAME}.service}"
DASHBOARD_SERVICE_NAME="${TB_DASHBOARD_SERVICE_NAME:-token-dashboard}"
DASHBOARD_SERVICE_FILE="${TB_DASHBOARD_SERVICE_FILE:-$SYSTEMD_USER_DIR/${DASHBOARD_SERVICE_NAME}.service}"
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

    # GUI/user services do not inherit an interactive shell's activated conda
    # environment. Persist the first interpreter that can really run the app.
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

command -v curl >/dev/null 2>&1 || {
    echo "[ERROR] curl not found" >&2
    exit 1
}
PYTHON_BIN="$(select_python)"
mkdir -p "$DATA_DIR"

USE_SYSTEMD=true
if [ "${TB_NO_SYSTEMD:-0}" = "1" ]; then USE_SYSTEMD=false; fi
HAS_SYSTEMD=false
if $USE_SYSTEMD && command -v systemctl >/dev/null 2>&1 &&
   systemctl --user daemon-reload 2>/dev/null; then
    HAS_SYSTEMD=true
fi

EXIT_STATUS=0
STARTUP_COMMITTED=false
FALLBACK_DASHBOARD_WAS_ACTIVE=false
FALLBACK_PROXY_WAS_ACTIVE=false

DASHBOARD_SERVICE_EXISTED=false
DASHBOARD_SERVICE_WAS_ENABLED=false
DASHBOARD_SERVICE_WAS_ACTIVE=false
DASHBOARD_SERVICE_TOUCHED=false
DASHBOARD_SERVICE_BACKUP=""
PROXY_SERVICE_EXISTED=false
PROXY_SERVICE_WAS_ENABLED=false
PROXY_SERVICE_WAS_ACTIVE=false
PROXY_SERVICE_TOUCHED=false
PROXY_SERVICE_BACKUP=""
LEGACY_TIMER_WAS_ENABLED=false
LEGACY_TIMER_WAS_ACTIVE=false
LEGACY_SERVICE_WAS_ACTIVE=false
LEGACY_UNITS_TOUCHED=false
LEGACY_SERVICE_BACKUP=""
LEGACY_TIMER_BACKUP=""

restore_systemd_unit() {
    local name="$1" file="$2" existed="$3" was_enabled="$4"
    local was_active="$5" touched="$6" backup="$7"
    if [ "$touched" = true ]; then
        systemctl --user stop "$name" >/dev/null 2>&1 || true
        if [ -n "$backup" ] && [ -f "$backup" ]; then
            mv -f "$backup" "$file"
        elif [ "$existed" != true ]; then
            rm -f "$file"
        fi
        systemctl --user daemon-reload >/dev/null 2>&1 || true
    fi
    if [ "$was_enabled" = true ]; then
        systemctl --user enable "$name" >/dev/null 2>&1 || true
    else
        systemctl --user disable "$name" >/dev/null 2>&1 || true
    fi
    if [ "$was_active" = true ]; then
        systemctl --user start "$name" >/dev/null 2>&1 || true
    else
        systemctl --user stop "$name" >/dev/null 2>&1 || true
    fi
}

cleanup() {
    trap - EXIT INT TERM
    if [ -n "${DASHBOARD_PID:-}" ] && kill -0 "$DASHBOARD_PID" 2>/dev/null; then
        echo ""
        echo "[INFO] 正在关闭仪表板..."
        kill "$DASHBOARD_PID" 2>/dev/null || true
        sleep 1
        if kill -0 "$DASHBOARD_PID" 2>/dev/null; then
            kill -9 "$DASHBOARD_PID" 2>/dev/null || true
        fi
    fi
    rm -f "${DASHBOARD_PID_FILE:-}"
    if [ -n "${PROXY_PID:-}" ] && kill -0 "$PROXY_PID" 2>/dev/null; then
        kill "$PROXY_PID" 2>/dev/null || true
        sleep 1
        if kill -0 "$PROXY_PID" 2>/dev/null; then
            kill -9 "$PROXY_PID" 2>/dev/null || true
        fi
    fi
    rm -f "${PROXY_PID_FILE:-}"
    if [ "${EXIT_STATUS:-0}" != "0" ] && [ "$STARTUP_COMMITTED" != true ]; then
        if $HAS_SYSTEMD; then
            restore_systemd_unit "$DASHBOARD_SERVICE_NAME" \
                "$DASHBOARD_SERVICE_FILE" "$DASHBOARD_SERVICE_EXISTED" \
                "$DASHBOARD_SERVICE_WAS_ENABLED" "$DASHBOARD_SERVICE_WAS_ACTIVE" \
                "$DASHBOARD_SERVICE_TOUCHED" "$DASHBOARD_SERVICE_BACKUP"
            restore_systemd_unit "$PROXY_SERVICE_NAME" "$PROXY_SERVICE_FILE" \
                "$PROXY_SERVICE_EXISTED" "$PROXY_SERVICE_WAS_ENABLED" \
                "$PROXY_SERVICE_WAS_ACTIVE" "$PROXY_SERVICE_TOUCHED" \
                "$PROXY_SERVICE_BACKUP"
            if [ "$LEGACY_UNITS_TOUCHED" = true ]; then
                [ -z "$LEGACY_SERVICE_BACKUP" ] ||
                    mv -f "$LEGACY_SERVICE_BACKUP" "$LEGACY_IMPORT_SERVICE_FILE"
                [ -z "$LEGACY_TIMER_BACKUP" ] ||
                    mv -f "$LEGACY_TIMER_BACKUP" "$LEGACY_IMPORT_TIMER_FILE"
                systemctl --user daemon-reload >/dev/null 2>&1 || true
            fi
            if [ "$LEGACY_TIMER_WAS_ENABLED" = true ]; then
                systemctl --user enable "$LEGACY_IMPORT_NAME.timer" \
                    >/dev/null 2>&1 || true
            else
                systemctl --user disable "$LEGACY_IMPORT_NAME.timer" \
                    >/dev/null 2>&1 || true
            fi
            if [ "$LEGACY_TIMER_WAS_ACTIVE" = true ]; then
                systemctl --user start "$LEGACY_IMPORT_NAME.timer" \
                    >/dev/null 2>&1 || true
            else
                systemctl --user stop "$LEGACY_IMPORT_NAME.timer" \
                    >/dev/null 2>&1 || true
            fi
            if [ "$LEGACY_SERVICE_WAS_ACTIVE" = true ]; then
                systemctl --user start "$LEGACY_IMPORT_NAME.service" \
                    >/dev/null 2>&1 || true
            else
                systemctl --user stop "$LEGACY_IMPORT_NAME.service" \
                    >/dev/null 2>&1 || true
            fi
        fi
        if [ "$FALLBACK_DASHBOARD_WAS_ACTIVE" = true ]; then
            launch_fallback_dashboard >/dev/null 2>&1 || true
        fi
        if [ "$FALLBACK_PROXY_WAS_ACTIVE" = true ]; then
            launch_fallback_proxy >/dev/null 2>&1 || true
        fi
    fi
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
trap 'EXIT_STATUS=130; cleanup' INT TERM

wait_for_url() {
    local url="$1"
    local attempts="$2"
    local i
    for ((i = 1; i <= attempts; i++)); do
        if curl -fsS "$url" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    return 1
}

wait_for_importer() {
    local url="$1"
    local attempts="$2"
    local payload
    for ((i = 1; i <= attempts; i++)); do
        payload="$(curl -fsS "$url" 2>/dev/null || true)"
        if [ -n "$payload" ] && "$PYTHON_BIN" -c '
import json, sys
payload = json.loads(sys.argv[1])
tasks = payload.get("background_tasks", {})
raise SystemExit(0 if tasks.get("codex-importer", {}).get("status") == "ok" else 1)
' "$payload" 2>/dev/null; then
            return 0
        fi
        sleep 1
    done
    return 1
}

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

process_command() {
    local pid="$1"
    if [ -r "/proc/$pid/cmdline" ]; then
        tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true
    else
        ps -p "$pid" -o command= 2>/dev/null || true
    fi
}

process_cwd() {
    local pid="$1"
    if [ -L "/proc/$pid/cwd" ]; then
        readlink "/proc/$pid/cwd" 2>/dev/null || true
    fi
}

is_managed_dashboard_pid() {
    local pid="$1" command cwd
    command="$(process_command "$pid")"
    if [[ "$command" == *"$SCRIPT_DIR/server.py"* ]]; then
        return 0
    fi
    cwd="$(process_cwd "$pid")"
    [[ "$command" == *"server.py"* && "$cwd" = "$SCRIPT_DIR" ]]
}

is_managed_proxy_pid() {
    local pid="$1" command
    command="$(process_command "$pid")"
    [[ "$command" == *"$PROXY_BIN"* ]]
}

stop_managed_pid() {
    local pid="$1" label="$2"
    kill "$pid" 2>/dev/null || true
    for ((i = 1; i <= 15; i++)); do
        if ! kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
        sleep 1
    done
    echo "[${label}] 进程未在 15 秒内退出，强制停止 (PID: $pid)" >&2
    kill -9 "$pid" 2>/dev/null || true
    ! kill -0 "$pid" 2>/dev/null
}

launch_fallback_dashboard() {
    "$PYTHON_BIN" "$SCRIPT_DIR/server.py" --port "$DASHBOARD_PORT" \
        --proxy-db "$PROXY_DB" --schema-dir "$SCHEMA_DIR" \
        --host 127.0.0.1 >/dev/null 2>&1 &
    local pid=$!
    printf '%s\n' "$pid" > "$DASHBOARD_PID_FILE"
    echo "$pid"
}

launch_fallback_proxy() {
    "$PROXY_BIN" --db "$PROXY_DB" --schema-dir "$SCHEMA_DIR" \
        --host 127.0.0.1 --port "$PROXY_PORT" >/dev/null 2>&1 &
    local pid=$!
    printf '%s\n' "$pid" > "$PROXY_PID_FILE"
    echo "$pid"
}

echo "============================================"
echo "  Token Board"
echo "============================================"
echo ""

# Stop database users before the schema coordinator runs. A major upgrade
# atomically replaces database files, so allowing an old process to keep an
# open inode could lose writes made during migration.
if $HAS_SYSTEMD; then
    if [ -f "$DASHBOARD_SERVICE_FILE" ]; then
        DASHBOARD_SERVICE_EXISTED=true
    fi
    if systemctl --user is-enabled --quiet "$DASHBOARD_SERVICE_NAME"; then
        DASHBOARD_SERVICE_WAS_ENABLED=true
    fi
    if systemctl --user is-active --quiet "$DASHBOARD_SERVICE_NAME"; then
        DASHBOARD_SERVICE_WAS_ACTIVE=true
        systemctl --user stop "$DASHBOARD_SERVICE_NAME"
    fi
    if [ -f "$PROXY_SERVICE_FILE" ]; then
        PROXY_SERVICE_EXISTED=true
    fi
    if systemctl --user is-enabled --quiet "$PROXY_SERVICE_NAME"; then
        PROXY_SERVICE_WAS_ENABLED=true
    fi
    if systemctl --user is-active --quiet "$PROXY_SERVICE_NAME"; then
        PROXY_SERVICE_WAS_ACTIVE=true
        systemctl --user stop "$PROXY_SERVICE_NAME"
    fi
else
    if [ -f "$DASHBOARD_PID_FILE" ]; then
        EXISTING_DASHBOARD="$(sed -n '1p' "$DASHBOARD_PID_FILE" 2>/dev/null || true)"
        if [[ "$EXISTING_DASHBOARD" =~ ^[0-9]+$ ]] &&
           kill -0 "$EXISTING_DASHBOARD" 2>/dev/null; then
            if ! is_managed_dashboard_pid "$EXISTING_DASHBOARD"; then
                echo "[ERROR] dashboard.pid 指向非本项目进程，拒绝停止: $EXISTING_DASHBOARD" >&2
                exit 1
            fi
            FALLBACK_DASHBOARD_WAS_ACTIVE=true
            stop_managed_pid "$EXISTING_DASHBOARD" dashboard
        fi
        rm -f "$DASHBOARD_PID_FILE"
    fi
    if [ -f "$PROXY_PID_FILE" ]; then
        EXISTING_PROXY="$(sed -n '1p' "$PROXY_PID_FILE" 2>/dev/null || true)"
        if [[ "$EXISTING_PROXY" =~ ^[0-9]+$ ]] &&
           kill -0 "$EXISTING_PROXY" 2>/dev/null; then
            if ! is_managed_proxy_pid "$EXISTING_PROXY"; then
                echo "[ERROR] token_proxy.pid 指向非本项目进程，拒绝停止: $EXISTING_PROXY" >&2
                exit 1
            fi
            FALLBACK_PROXY_WAS_ACTIVE=true
            stop_managed_pid "$EXISTING_PROXY" proxy
        fi
        rm -f "$PROXY_PID_FILE"
    fi
fi

# Pause the old separate timer while the integrated service is being prepared.
# It remains installed/enabled until the new dashboard passes its health check,
# and cleanup restores it if any earlier startup step fails.
if $HAS_SYSTEMD; then
    if systemctl --user is-active --quiet "$LEGACY_IMPORT_NAME.timer"; then
        LEGACY_TIMER_WAS_ACTIVE=true
    fi
    if systemctl --user is-enabled --quiet "$LEGACY_IMPORT_NAME.timer"; then
        LEGACY_TIMER_WAS_ENABLED=true
    fi
    if systemctl --user is-active --quiet "$LEGACY_IMPORT_NAME.service"; then
        LEGACY_SERVICE_WAS_ACTIVE=true
    fi
    if [ "$LEGACY_TIMER_WAS_ACTIVE" = true ] ||
       [ "$LEGACY_TIMER_WAS_ENABLED" = true ]; then
        if ! systemctl --user stop "$LEGACY_IMPORT_NAME.timer" \
                >/dev/null 2>&1; then
            echo "[ERROR] 无法停止旧用量导入定时器，取消迁移" >&2
            exit 1
        fi
    fi
    if [ "$LEGACY_SERVICE_WAS_ACTIVE" = true ]; then
        if ! systemctl --user stop "$LEGACY_IMPORT_NAME.service" \
                >/dev/null 2>&1; then
            echo "[ERROR] 无法停止旧用量导入服务，取消迁移" >&2
            exit 1
        fi
    fi
fi

echo "[schema] 检查并自动升级本地数据库..."
PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON_BIN" -m app.db.schema_upgrade.cli \
    --proxy-db "$PROXY_DB" --dashboard-db "$DASHBOARD_DB" \
    --schema-dir "$SCHEMA_DIR" --timezone "$LEGACY_TIMEZONE"
echo -e "${GREEN}✓ 本地数据库已准备${NC}"

# A plain dashboard launch does not replace the proxy unit.  If it was running
# before the safe migration window, resume it now.
if $HAS_SYSTEMD && $PROXY_SERVICE_WAS_ACTIVE && ! $START_ALL; then
    systemctl --user start "$PROXY_SERVICE_NAME"
fi
if ! $HAS_SYSTEMD && $FALLBACK_PROXY_WAS_ACTIVE && ! $START_ALL; then
    PROXY_PID="$(launch_fallback_proxy)"
fi

# ═══════════════════════════════════════════════════════════════════════
# Optional C++ proxy setup (--all)
# ═══════════════════════════════════════════════════════════════════════

if $START_ALL; then
    command -v cmake >/dev/null 2>&1 || {
        echo "[ERROR] cmake not found" >&2
        exit 1
    }

    echo "[proxy] 编译 C++ 代理..."
    cmake -S "$SCRIPT_DIR/proxy" -B "$SCRIPT_DIR/proxy/build" \
        -DCMAKE_BUILD_TYPE=Release >/dev/null 2>&1
    BUILD_JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"
    cmake --build "$SCRIPT_DIR/proxy/build" -j"$BUILD_JOBS" >/dev/null 2>&1
    [ -x "$PROXY_BIN" ] || {
        echo "[ERROR] proxy binary not found: $PROXY_BIN" >&2
        exit 1
    }
    echo -e "${GREEN}✓ 编译完成${NC}"

    if $HAS_SYSTEMD; then
        echo "[proxy] 更新 systemd 服务（开机自启）..."
        mkdir -p "$(dirname "$PROXY_SERVICE_FILE")"
        PROXY_SERVICE_BACKUP=""
        if [ -f "$PROXY_SERVICE_FILE" ]; then
            PROXY_SERVICE_BACKUP="$PROXY_SERVICE_FILE.backup.$$"
            cp -p "$PROXY_SERVICE_FILE" "$PROXY_SERVICE_BACKUP"
        fi
        PROXY_SERVICE_TMP="$PROXY_SERVICE_FILE.tmp.$$"
        cat > "$PROXY_SERVICE_TMP" <<EOF
[Unit]
Description=Token Board API Proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart="$PROXY_BIN" --db "$PROXY_DB" --schema-dir "$SCHEMA_DIR" --host 127.0.0.1 --port $PROXY_PORT
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF
        PROXY_SERVICE_TOUCHED=true
        mv -f "$PROXY_SERVICE_TMP" "$PROXY_SERVICE_FILE"
        systemctl --user daemon-reload
        systemctl --user enable "$PROXY_SERVICE_NAME" >/dev/null 2>&1
        systemctl --user restart "$PROXY_SERVICE_NAME"
        echo -e "${GREEN}✓ 代理已启动并设置开机自启${NC}"
    else
        launch_fallback_proxy >/dev/null
        PROXY_PID="$(sed -n '1p' "$PROXY_PID_FILE")"
        echo -e "${GREEN}✓ 代理已启动 (PID: $PROXY_PID)${NC}"
    fi

    echo "[proxy] 等待健康检查..."
    PROXY_READY=false
    for ((i = 1; i <= 30; i++)); do
        HEALTH_JSON=""
        if $HAS_SYSTEMD || kill -0 "$PROXY_PID" 2>/dev/null; then
            HEALTH_JSON="$(curl -fsS "http://127.0.0.1:$PROXY_PORT/health" 2>/dev/null || true)"
        fi
        if [ -n "$HEALTH_JSON" ] && "$PYTHON_BIN" -c '
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
    echo -e "${GREEN}✓ 代理健康: http://localhost:$PROXY_PORT/v1${NC}"
    echo ""
fi

# ═══════════════════════════════════════════════════════════════════════
# Python dashboard + integrated agent usage importer
# ═══════════════════════════════════════════════════════════════════════

if [ -f "$DASHBOARD_PID_FILE" ]; then
    EXISTING_DASHBOARD="$(sed -n '1p' "$DASHBOARD_PID_FILE" 2>/dev/null || true)"
    if [[ "$EXISTING_DASHBOARD" =~ ^[0-9]+$ ]] &&
       kill -0 "$EXISTING_DASHBOARD" 2>/dev/null; then
        if ! is_managed_dashboard_pid "$EXISTING_DASHBOARD"; then
            echo "[ERROR] dashboard.pid 指向非本项目进程，拒绝停止: $EXISTING_DASHBOARD" >&2
            exit 1
        fi
        echo "[dash] 关闭旧的前台仪表板进程..."
        stop_managed_pid "$EXISTING_DASHBOARD" dashboard
    fi
    rm -f "$DASHBOARD_PID_FILE"
fi

DASHBOARD_URL="http://localhost:$DASHBOARD_PORT"
if $HAS_SYSTEMD; then
    echo "[dash] 更新 systemd 服务（默认开机自启）..."
    mkdir -p "$(dirname "$DASHBOARD_SERVICE_FILE")"
    DASHBOARD_SERVICE_BACKUP=""
    if [ -f "$DASHBOARD_SERVICE_FILE" ]; then
        DASHBOARD_SERVICE_BACKUP="$DASHBOARD_SERVICE_FILE.backup.$$"
        cp -p "$DASHBOARD_SERVICE_FILE" "$DASHBOARD_SERVICE_BACKUP"
    fi
    DASHBOARD_SERVICE_TMP="$DASHBOARD_SERVICE_FILE.tmp.$$"
    cat > "$DASHBOARD_SERVICE_TMP" <<EOF
[Unit]
Description=Token Board Dashboard and Agent Usage Importer
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
Environment="PYTHONPATH=$SCRIPT_DIR"
ExecStart="$PYTHON_BIN" "$SCRIPT_DIR/server.py" --port $DASHBOARD_PORT --proxy-db "$PROXY_DB" --schema-dir "$SCHEMA_DIR" --host 127.0.0.1
Restart=on-failure
RestartSec=5
TimeoutStopSec=15
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF
    DASHBOARD_SERVICE_TOUCHED=true
    mv -f "$DASHBOARD_SERVICE_TMP" "$DASHBOARD_SERVICE_FILE"
    systemctl --user daemon-reload
    systemctl --user enable "$DASHBOARD_SERVICE_NAME" >/dev/null 2>&1
    systemctl --user restart "$DASHBOARD_SERVICE_NAME"

    if ! wait_for_url "$DASHBOARD_URL/api/summary" 30 ||
       ! systemctl --user is-active --quiet "$DASHBOARD_SERVICE_NAME" ||
       ! wait_for_importer "$DASHBOARD_URL/api/proxy/perf/realtime" 30; then
        echo "[ERROR] dashboard health check failed" >&2
        echo "        查看日志: journalctl --user -u $DASHBOARD_SERVICE_NAME -n 50" >&2
        EXIT_STATUS=1
        cleanup
    fi
    echo -e "${GREEN}✓ 仪表板已启动并设置开机自启${NC}"

    # The replacement is healthy; now retire the old split importer for good.
    if [ -f "$LEGACY_IMPORT_SERVICE_FILE" ]; then
        LEGACY_SERVICE_BACKUP="$LEGACY_IMPORT_SERVICE_FILE.backup.$$"
        cp -p "$LEGACY_IMPORT_SERVICE_FILE" "$LEGACY_SERVICE_BACKUP"
    fi
    if [ -f "$LEGACY_IMPORT_TIMER_FILE" ]; then
        LEGACY_TIMER_BACKUP="$LEGACY_IMPORT_TIMER_FILE.backup.$$"
        cp -p "$LEGACY_IMPORT_TIMER_FILE" "$LEGACY_TIMER_BACKUP"
    fi
    LEGACY_UNITS_TOUCHED=true
    if [ "$LEGACY_TIMER_WAS_ACTIVE" = true ] ||
       [ "$LEGACY_TIMER_WAS_ENABLED" = true ]; then
        if ! systemctl --user disable --now "$LEGACY_IMPORT_NAME.timer" \
                >/dev/null 2>&1; then
            echo "[ERROR] 无法禁用旧用量导入定时器" >&2
            EXIT_STATUS=1
            cleanup
        fi
    fi
    if [ "$LEGACY_SERVICE_WAS_ACTIVE" = true ]; then
        if ! systemctl --user stop "$LEGACY_IMPORT_NAME.service" \
                >/dev/null 2>&1; then
            echo "[ERROR] 无法停止旧用量导入服务" >&2
            EXIT_STATUS=1
            cleanup
        fi
    fi
    if [ -f "$LEGACY_IMPORT_SERVICE_FILE" ] ||
       [ -f "$LEGACY_IMPORT_TIMER_FILE" ]; then
        echo "[import] 移除旧的独立用量导入定时器..."
        rm -f "$LEGACY_IMPORT_SERVICE_FILE" "$LEGACY_IMPORT_TIMER_FILE"
        systemctl --user daemon-reload
    fi
    rm -f "$LEGACY_SERVICE_BACKUP" "$LEGACY_TIMER_BACKUP" || true
    rm -f "$DASHBOARD_SERVICE_BACKUP" "$PROXY_SERVICE_BACKUP" || true
    DASHBOARD_SERVICE_BACKUP=""
    PROXY_SERVICE_BACKUP=""
    STARTUP_COMMITTED=true
    echo -e "${GREEN}✓ 用量导入已合并到仪表板服务${NC}"

    if command -v loginctl >/dev/null 2>&1; then
        LINGER_STATE="$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || true)"
        if [ "$LINGER_STATE" = "no" ]; then
            echo -e "${YELLOW}[提示] 当前服务会在登录后自启；若需未登录也随开机启动，请执行:${NC}"
            echo "       sudo loginctl enable-linger $(id -un)"
        fi
    fi

    echo ""
    echo "  ➜  仪表板: $DASHBOARD_URL"
    echo "  ➜  Agent 用量: 随服务启动、每 30 分钟、每次打开网页时采集"
    echo ""
    open_browser "$DASHBOARD_URL"
    exit 0
fi

echo "[dash] 未检测到可用的 systemd 用户服务，改以前台方式启动..."
echo "       Ctrl+C 关闭"
cd "$SCRIPT_DIR"
launch_fallback_dashboard >/dev/null
DASHBOARD_PID="$(sed -n '1p' "$DASHBOARD_PID_FILE")"

if ! kill -0 "$DASHBOARD_PID" 2>/dev/null ||
   ! wait_for_url "$DASHBOARD_URL/api/summary" 30 ||
   ! wait_for_importer "$DASHBOARD_URL/api/proxy/perf/realtime" 30; then
    echo "[ERROR] dashboard health check failed" >&2
    EXIT_STATUS=1
    cleanup
fi

echo ""
echo "  ➜  仪表板: $DASHBOARD_URL"
echo "  ➜  Agent 用量: 随服务启动、每 30 分钟、每次打开网页时采集"
echo ""
open_browser "$DASHBOARD_URL"
STARTUP_COMMITTED=true
wait "$DASHBOARD_PID"
