#!/usr/bin/env bash
# One-time (or refresh) seed of C:\ci\EEmovel-Backend on the eemovel-windows-ci
# VM, from the host's local clone - no GitHub credential ever enters the VM.
#
#   seed-windows-ci-clone.sh            fresh seed (fails if the clone exists)
#   seed-windows-ci-clone.sh --refresh  only re-ship the bundle, so the
#                                       runner's `git fetch origin <branch>`
#                                       sees commits made since the last seed
#
# The VM clone's `origin` is C:/ci/seed.bundle, so refreshing = overwriting
# that file. The bundle carries the host clone's *local* branches: update the
# branch you want to test there first (e.g.
# `git fetch origin refs/heads/hml:refs/heads/hml`).
#
# `nuget restore` is required, not optional: the solution mixes SDK-style
# projects (PackageReference, restored by MSBuild /restore) with legacy
# packages.config projects, which need .\packages\ populated or the build
# dies with CS0006 on the AWSSDK analyzers.
set -euo pipefail

HOST_CLONE="${HOST_CLONE:-/mnt/projetos/Projects/Brokers/EEmovel-Backend}"
VM_DIR="$HOME/.config/acp-bridge/windows-ci"
VM_KNOWN_HOSTS="$VM_DIR/known_hosts"
# shellcheck disable=SC1091
source "$VM_DIR/.env"

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$VM_KNOWN_HOSTS" -o ConnectTimeout=10)
ssh_run() {
    sshpass -p "$VM_PASSWORD" ssh -p 2222 "${SSH_OPTS[@]}" ci@127.0.0.1 "$1"
}

date +%s > "$VM_DIR/.last-used"

bundle=$(mktemp --suffix=.bundle)
trap 'rm -f "$bundle"' EXIT
echo "[seed] bundling $HOST_CLONE (all refs)"
git -C "$HOST_CLONE" bundle create "$bundle" --all 2>/dev/null
ls -lh "$bundle" | awk '{print "[seed] bundle size: " $5}'

ssh_run "if not exist C:\\ci mkdir C:\\ci"
sshpass -p "$VM_PASSWORD" scp -P 2222 "${SSH_OPTS[@]}" -q "$bundle" ci@127.0.0.1:C:/ci/seed.bundle
echo "[seed] bundle copied to C:\\ci\\seed.bundle"

if [ "${1:-}" = "--refresh" ]; then
    echo "[seed] refresh done"
    exit 0
fi

if ssh_run "if exist C:\\ci\\EEmovel-Backend (exit 0) else (exit 1)"; then
    echo "[error] C:\\ci\\EEmovel-Backend already exists - use --refresh, or remove it first"
    exit 2
fi

echo "[seed] cloning from bundle"
ssh_run "cd /d C:\\ci && git clone --quiet C:/ci/seed.bundle EEmovel-Backend"
echo "[seed] nuget restore (packages.config projects)"
ssh_run "cd /d C:\\ci\\EEmovel-Backend && C:\\Windows\\nuget.exe restore EEmovelDev.sln -NonInteractive -Verbosity quiet"
echo "[seed] done"
