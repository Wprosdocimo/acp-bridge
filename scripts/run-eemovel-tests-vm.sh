#!/usr/bin/env bash
# Fixed, non-agentic test runner for EEmovel-Backend (.NET Framework 4.8.1),
# via SSH into the eemovel-windows-ci VM (dockur/windows) instead of WSL
# interop. Sibling of scripts/run-eemovel-tests.sh (config.winterop.yaml,
# port 18012) - same argument contract, same safety posture (fixed script,
# allowlisted input, dedicated clone never touched by hand), different
# transport: WSL interop doesn't exist outside WSL, SSH does.
#
# Full background / gotchas (Web.config preservation, packages/ restore
# chicken-and-egg, baseline testing practice): ai-memory
# procedural/eemovel-backend-build-test-windows-interop.md (Playground project).
set -euo pipefail

VM_HOST="127.0.0.1"
VM_PORT="2222"
VM_USER="ci"
VM_DIR="$HOME/.config/acp-bridge/windows-ci"
VM_ENV_FILE="$VM_DIR/.env"
CI_DIR_WIN='C:\ci\EEmovel-Backend'
MSBUILD='C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\MSBuild\Current\Bin\amd64\MSBuild.exe'
# vstest.console.exe doesn't ship with VS Build Tools even with the
# TestTools.Core component (confirmed absent anywhere under BuildTools) -
# `dotnet vstest` is Microsoft's own documented no-VS-install path and works
# fine against a net481-targeted test DLL despite dotnet itself being a
# .NET (Core+) host; confirmed live, 471/510 passing matching the known
# baseline (see ai-memory procedural doc).
VSTEST='dotnet vstest'
TEST_DLL='C:\ci\EEmovel-Backend\EEmovel.Unit.Tests\bin\Debug\net481\EEmovel.Unit.Tests.dll'
# Cold boot of Windows Server under QEMU takes well over 3 min to get sshd
# up (180s here caused an up -> give up -> retry loop on 2026-09-22).
BOOT_TIMEOUT_S=900
# Dedicated known_hosts: a rebuilt VM gets new host keys, and accept-new
# refuses a *changed* key (looks like "VM not provisioned"). Scoped to this
# loopback-only VM so it can be reset without touching ~/.ssh/known_hosts.
VM_KNOWN_HOSTS="$HOME/.config/acp-bridge/windows-ci/known_hosts"

if [ ! -f "$VM_ENV_FILE" ]; then
    echo "[error] $VM_ENV_FILE not found - VM not provisioned yet"
    exit 2
fi
# shellcheck disable=SC1090
source "$VM_ENV_FILE"
if [ -z "${VM_PASSWORD:-}" ]; then
    echo "[error] VM_PASSWORD not set in $VM_ENV_FILE"
    exit 2
fi

# One run at a time: every job shares the single clone on the VM (and the
# seed bundle), so two concurrent jobs (pool max_per_agent allows 2) switched
# branches or overwrote the bundle under each other. A second job waits here.
exec 9>"$VM_DIR/.runner.lock"
if ! flock -w 3600 9; then
    echo "[error] timed out waiting for another eemovel-tests-vm run to finish"
    exit 5
fi

ssh_run() {
    sshpass -p "$VM_PASSWORD" ssh -p "$VM_PORT" \
        -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$VM_KNOWN_HOSTS" -o ConnectTimeout=10 \
        "$VM_USER@$VM_HOST" "$1"
}

vm_ssh_reachable() {
    ssh_run "echo ok" >/dev/null 2>&1
}

# ssh_run's plain string passes through bash -> ssh argv -> Win32-OpenSSH's
# cmd.exe wrapper, and backslash-heavy Windows paths + quoting do not survive
# that trip intact (confirmed live: a `more > file` redirect trick silently
# truncated file content at the first embedded quote). -EncodedCommand side-
# steps this entirely: the whole PowerShell script becomes a single
# base64/UTF-16LE token with no shell metacharacters left to mangle.
ssh_ps() {
    local ps_b64
    ps_b64=$(printf '%s' "$1" | iconv -f UTF-8 -t UTF-16LE | base64 -w0)
    sshpass -p "$VM_PASSWORD" ssh -p "$VM_PORT" \
        -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$VM_KNOWN_HOSTS" -o ConnectTimeout=10 \
        "$VM_USER@$VM_HOST" "powershell -NoProfile -NonInteractive -EncodedCommand $ps_b64"
}

write_remote_file() {
    local content="$1" remote_path="$2" b64content
    # %s, not %b - content already has real CR/LF bytes; %b would
    # reinterpret literal backslashes in Windows paths (see CRLF note above).
    b64content=$(printf '%s' "$content" | base64 -w0)
    ssh_ps "[IO.File]::WriteAllBytes('$remote_path', [Convert]::FromBase64String('$b64content'))"
}

# Start the VM on demand instead of keeping it running 24/7 - it stays up
# after this for fast sequential calls, and can be stopped by hand
# (scripts/stop-windows-ci.sh) when nobody's working on EEmovel-Backend.
# The disk is persisted (docker-compose bind mount), so this resumes the
# already-installed Windows, it never re-runs setup/provisioning.
mark_used() { date +%s > "$VM_DIR/.last-used"; }
# Marks activity for the idle-shutdown watcher (scripts/idle-check-windows-ci.sh)
# BEFORE starting the VM and on every wait iteration - marking only after the
# boot let the watchdog kill a VM idle for 1h+ in the middle of its boot,
# forever (deadlock found 2026-09-22).
mark_used
if ! vm_ssh_reachable; then
    # A rebuilt VM has new host keys; drop the stale entry once and retry.
    if sshpass -p "$VM_PASSWORD" ssh -p "$VM_PORT" -o StrictHostKeyChecking=accept-new \
        -o UserKnownHostsFile="$VM_KNOWN_HOSTS" -o ConnectTimeout=10 "$VM_USER@$VM_HOST" "echo ok" 2>&1 \
        | grep -q "REMOTE HOST IDENTIFICATION HAS CHANGED"; then
        echo "[run-eemovel-tests-vm] VM host key changed (rebuilt VM), resetting $VM_KNOWN_HOSTS entry"
        ssh-keygen -q -f "$VM_KNOWN_HOSTS" -R "[$VM_HOST]:$VM_PORT" >/dev/null 2>&1 || true
    fi
fi
if ! vm_ssh_reachable; then
    # Only `up -d` when the container is not running: re-running it against a
    # VM that is still booting recreated the container and restarted the
    # boot from zero (cost ~25 min on 2026-09-22).
    if [ -z "$(cd "$VM_DIR" && docker compose ps --status running --quiet windows-ci 2>/dev/null)" ]; then
        echo "[run-eemovel-tests-vm] VM not running, starting it (docker compose up -d)..."
        (cd "$VM_DIR" && docker compose up -d) >/dev/null
    else
        echo "[run-eemovel-tests-vm] VM container already running, waiting for sshd..."
    fi
    waited=0
    until vm_ssh_reachable; do
        if [ "$waited" -ge "$BOOT_TIMEOUT_S" ]; then
            echo "[error] VM did not come up within ${BOOT_TIMEOUT_S}s"
            exit 3
        fi
        mark_used
        sleep 5
        waited=$((waited + 5))
    done
    echo "[run-eemovel-tests-vm] VM up after ${waited}s"
fi
mark_used

# Only variable input accepted: whitespace-separated key=value tokens, only
# "branch=" and "filter=" recognized - both validated against a strict
# allowlist before ever reaching a remote command, same as
# run-eemovel-tests.sh (see that script for the injection-surface rationale;
# it applies identically here, the value still ends up literal inside a
# remote .bat).
BRANCH=""
TEST_FILTER=""
REFRESH=""
for token in "${1:-}"; do
    [ -z "$token" ] && continue
    for kv in $token; do
        case "$kv" in
            refresh=1)
                # Re-ship the seed bundle before fetching, so commits/branches
                # made in the host clone since the last seed reach the VM.
                REFRESH=1
                ;;
            branch=*)
                BRANCH="${kv#branch=}"
                if ! [[ "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]]; then
                    echo "[error] branch contains disallowed characters (only letters, digits, . _ / - are accepted): $BRANCH"
                    exit 4
                fi
                ;;
            filter=*)
                TEST_FILTER="${kv#filter=}"
                if ! [[ "$TEST_FILTER" =~ ^[A-Za-z0-9_.~=]+$ ]]; then
                    echo "[error] test filter contains disallowed characters (only letters, digits, . _ ~ = are accepted): $TEST_FILTER"
                    exit 4
                fi
                ;;
            *)
                echo "[error] unrecognized token (expected refresh=1, branch=... and/or filter=...): $kv"
                exit 4
                ;;
        esac
    done
done

if ! ssh_run "if exist C:\\ci\\EEmovel-Backend (exit 0) else (exit 1)" ; then
    echo "[error] CI clone not found at $CI_DIR_WIN on the VM - run the one-time seed:"
    echo "        scripts/seed-windows-ci-clone.sh (git bundle + nuget restore)"
    exit 2
fi

if [ -n "$REFRESH" ]; then
    echo "[run-eemovel-tests-vm] refreshing seed bundle from the host clone"
    "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/seed-windows-ci-clone.sh" --refresh
    mark_used
    if [ -z "$BRANCH" ]; then
        echo "[run-eemovel-tests-vm] refresh only (no branch=), skipping build/test"
        exit 0
    fi
fi

if [ -n "$BRANCH" ]; then
    echo "[run-eemovel-tests-vm] checking out branch: $BRANCH"
    # Same Web.config preservation gotcha as run-eemovel-tests.sh: it's a
    # gitignored, machine-local file that git still tracks pre-existing edits
    # to, and a hard reset would blow it away on every branch switch.
    ssh_run "copy /y C:\\ci\\EEmovel-Backend\\EEmovel.Web.Api\\Web.config %TEMP%\\_eemovel_webconfig_backup.tmp >nul"
    ssh_run "cd /d C:\\ci\\EEmovel-Backend && git fetch --quiet origin $BRANCH && git checkout --quiet -f $BRANCH && git reset --quiet --hard origin/$BRANCH"
    ssh_run "copy /y %TEMP%\\_eemovel_webconfig_backup.tmp C:\\ci\\EEmovel-Backend\\EEmovel.Web.Api\\Web.config >nul"
fi

if [ -n "$TEST_FILTER" ]; then
    FILTER_ARG="/TestCaseFilter:$TEST_FILTER"
else
    FILTER_ARG=""
fi

# NB: built with real CR/LF bytes ($CRLF), never literal "\r\n" text - a
# .bat path like C:\ci\... contains "\c", which printf/echo backslash-escape
# interpretation (%b, -e) reads as "stop output here" and silently truncates
# everything after it. Confirmed live: this is what broke the first two
# attempts at writing these files, regardless of the transport used.
CRLF=$'\r\n'
# VS Build Tools' own MSBuild doesn't ship the Microsoft.NET.Sdk resolver
# (that's a workload not present on this headless install - Community
# edition bundles it, Build Tools doesn't unless you get the exact right
# workload ID, which errored out silently rather than failing loudly when
# tried). Pointing MSBuildSDKsPath at the separately-installed .NET SDK's
# own Sdks folder resolves the SDK-style projects in this mixed solution
# without needing that workload at all. With MSBuildSDKsPath forced, the
# SDK's ImportWorkloads.props still asks for the workload resolver SDK
# (WorkloadAutoImportPropsLocator), which only exists when MSBuild finds the
# SDK itself - MSB4236 on .NET SDK 8.0.4xx (2026-09-23). The workloads are
# irrelevant for net481, so disable that resolver.
DOTNET_SDK_VERSION=$(ssh_run "dir /b \"C:\Program Files\dotnet\sdk\"" | tr -d '\r' | tail -n1)
DOTNET_SDKS_PATH="C:\\Program Files\\dotnet\\sdk\\${DOTNET_SDK_VERSION}\\Sdks"
# Exit code goes to its own small file, not appended after the redirected
# log (>> right after a > on the same path silently leaked to the SSH
# stdout instead of the file, in testing - a real, reproduced quirk, not
# theoretical. Separate files sidesteps it entirely.
BUILD_BAT_CONTENT="@echo off${CRLF}cd /d ${CI_DIR_WIN}${CRLF}set MSBuildSDKsPath=${DOTNET_SDKS_PATH}${CRLF}set MSBuildEnableWorkloadResolver=false${CRLF}\"${MSBUILD}\" EEmovelDev.sln /restore /p:Configuration=Debug /m /nologo > _eemovel_build.log 2>&1${CRLF}echo %ERRORLEVEL% > _eemovel_build.exitcode${CRLF}"
TEST_BAT_CONTENT="@echo off${CRLF}cd /d ${CI_DIR_WIN}${CRLF}${VSTEST} \"${TEST_DLL}\" ${FILTER_ARG} /Logger:trx > _eemovel_test.log 2>&1${CRLF}echo %ERRORLEVEL% > _eemovel_test.exitcode${CRLF}"

write_remote_file "$BUILD_BAT_CONTENT" 'C:\ci\EEmovel-Backend\_eemovel_build.bat'
write_remote_file "$TEST_BAT_CONTENT" 'C:\ci\EEmovel-Backend\_eemovel_test.bat'

echo "[run-eemovel-tests-vm] building ($CI_DIR_WIN)..."
ssh_ps "Remove-Item -ErrorAction SilentlyContinue 'C:\ci\EEmovel-Backend\_eemovel_build.log','C:\ci\EEmovel-Backend\_eemovel_build.exitcode'; & cmd /c 'C:\ci\EEmovel-Backend\_eemovel_build.bat'"

BUILD_LOG=$(ssh_run "type C:\\ci\\EEmovel-Backend\\_eemovel_build.log" 2>/dev/null || true)
BUILD_EXITCODE=$(ssh_run "type C:\\ci\\EEmovel-Backend\\_eemovel_build.exitcode" 2>/dev/null | tr -d '\r\n ' || true)
if [ -z "$BUILD_LOG" ] || [ -z "$BUILD_EXITCODE" ]; then
    echo "[error] build did not produce a log/exitcode file - SSH/build invocation likely failed"
    exit 3
fi
echo "$BUILD_LOG" | tail -n 6

if [ "$BUILD_EXITCODE" != "0" ]; then
    echo "[run-eemovel-tests-vm] build failed (exit $BUILD_EXITCODE), skipping tests"
    exit 1
fi

echo "[run-eemovel-tests-vm] running tests..."
ssh_ps "Remove-Item -ErrorAction SilentlyContinue 'C:\ci\EEmovel-Backend\_eemovel_test.log','C:\ci\EEmovel-Backend\_eemovel_test.exitcode'; & cmd /c 'C:\ci\EEmovel-Backend\_eemovel_test.bat'"

TEST_LOG=$(ssh_run "type C:\\ci\\EEmovel-Backend\\_eemovel_test.log" 2>/dev/null || true)
if [ -z "$TEST_LOG" ]; then
    echo "[error] test run did not produce a log file - SSH/test invocation likely failed"
    exit 3
fi

# dotnet vstest's summary line ("Failed! - Failed: N, Passed: N, ..." or
# "Passed! - Failed: 0, Passed: N, ..."), not vstest.console.exe's pt-BR
# "Total de testes:" - different tool, different (English) output format.
echo "$TEST_LOG" | grep -E "^(Passed|Failed)! - " || echo "$TEST_LOG" | tail -n 15
echo "[run-eemovel-tests-vm] full logs on the VM: C:\\ci\\EEmovel-Backend\\_eemovel_build.log , _eemovel_test.log"
