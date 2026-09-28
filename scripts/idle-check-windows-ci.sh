#!/usr/bin/env bash
# Stops the eemovel-windows-ci VM after IDLE_SECONDS of no test-runner
# activity (default 1h). Meant to be run periodically by
# acp-bridge-windows-ci-idle.timer. Safe to run when the VM is already
# stopped or was never used yet (no-ops).
set -euo pipefail
VM_DIR="$HOME/.config/acp-bridge/windows-ci"
LAST_USED_FILE="$VM_DIR/.last-used"
IDLE_SECONDS="${IDLE_SECONDS:-3600}"

if [ ! -f "$LAST_USED_FILE" ]; then
    exit 0
fi

last=$(cat "$LAST_USED_FILE")
now=$(date +%s)
idle=$((now - last))

if [ "$idle" -ge "$IDLE_SECONDS" ]; then
    if [ -n "$(cd "$VM_DIR" && docker compose ps --status running --quiet windows-ci 2>/dev/null)" ]; then
        echo "[idle-check] idle for ${idle}s (>= ${IDLE_SECONDS}s), stopping windows-ci"
        "$(dirname "$0")/stop-windows-ci.sh"
    fi
fi
