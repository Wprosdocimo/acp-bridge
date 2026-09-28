#!/usr/bin/env bash
# Stops the eemovel-windows-ci VM WITHOUT corrupting its state.
#
# Gotcha (found the hard way, 2026-09-18): `docker compose stop` defaults to
# a 10s timeout before SIGKILL-ing the container. dockur/windows translates
# SIGTERM into an ACPI shutdown request to the guest and waits up to ~100
# ticks for Windows to actually finish shutting down - Docker's default
# timeout kills it around tick 9/10, well before that. A SIGKILL mid-ACPI-
# shutdown corrupts the UEFI NVRAM (windows.vars), so on the next start the
# firmware loses its boot order and dockur/windows re-runs the ENTIRE
# unattended install from scratch (new SSH host keys are the tell - a fresh
# OpenSSH Server install always generates new ones). Confirmed reproduced.
#
# Fix: ask Windows itself to shut down first (clean, from inside the guest),
# wait for the container to exit on its own, and only fall back to
# `docker compose stop` with a generous timeout if that didn't work.
set -euo pipefail
cd "$HOME/.config/acp-bridge/windows-ci"
# shellcheck disable=SC1091
source .env

if docker compose ps --status running --quiet windows-ci >/dev/null 2>&1 && \
   [ -n "$(docker compose ps --status running --quiet windows-ci)" ]; then
    echo "[stop-windows-ci] asking Windows to shut down cleanly..."
    sshpass -p "$VM_PASSWORD" ssh -p 2222 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$HOME/.config/acp-bridge/windows-ci/known_hosts" -o ConnectTimeout=10 \
        ci@127.0.0.1 "shutdown /s /t 0" || true

    waited=0
    while [ -n "$(docker compose ps --status running --quiet windows-ci 2>/dev/null)" ]; do
        if [ "$waited" -ge 120 ]; then
            echo "[stop-windows-ci] guest shutdown did not finish in 120s, forcing (with a long timeout)"
            docker compose stop -t 180
            break
        fi
        sleep 5
        waited=$((waited + 5))
    done
    echo "[stop-windows-ci] stopped after ${waited}s"
else
    echo "[stop-windows-ci] already stopped"
fi
