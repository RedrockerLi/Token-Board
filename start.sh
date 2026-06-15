#!/usr/bin/env bash
# ==============================================================================
# Token Board — 一键启动（仪表板 + 代理）
# Usage: bash start.sh [--no-browser]
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "============================================"
echo "  Token Board"
echo "============================================"
echo ""

# ── Start proxy (background) ──
echo "[1/2] 启动代理..."
bash "$SCRIPT_DIR/scripts/start-proxy.sh" --daemon

# ── Start dashboard (foreground) ──
echo "[2/2] 启动仪表板..."
bash "$SCRIPT_DIR/scripts/start-dashboard.sh" "${@}"
