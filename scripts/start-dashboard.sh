#!/usr/bin/env bash
# Compatibility entry point. Dashboard startup is centralized in start.sh;
# the dashboard is a foreground process and no longer has a systemd owner.
set -Eeuo pipefail

SCRIPT_PATH="${BASH_SOURCE[0]}"
while [ -L "$SCRIPT_PATH" ]; do
    LINK_DIR="$(cd -P "$(dirname "$SCRIPT_PATH")" && pwd)"
    LINK_TARGET="$(readlink "$SCRIPT_PATH")"
    if [[ "$LINK_TARGET" = /* ]]; then
        SCRIPT_PATH="$LINK_TARGET"
    else
        SCRIPT_PATH="$LINK_DIR/$LINK_TARGET"
    fi
done
SCRIPT_DIR="$(cd -P "$(dirname "$SCRIPT_PATH")/.." && pwd)"
exec bash "$SCRIPT_DIR/start.sh" "$@"
