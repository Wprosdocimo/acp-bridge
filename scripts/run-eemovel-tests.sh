#!/usr/bin/env bash
# Fixed, non-agentic test runner for EEmovel-Backend (.NET Framework 4.8.1).
#
# Why this exists: EEmovel-Backend needs the real Windows MSBuild/vstest
# toolchain (WSL has no .NET Framework 4.8.1 reference assemblies). Calling
# Windows binaries requires WSL interop, which is architecturally incompatible
# with ai-jail's PID-namespace sandboxing (bwrap becomes a fake PID 1, breaks
# WSL's UtilGetPpid()/proc/1/stat assumption — confirmed live 2026-09-03, not
# a missing --map). Since this can't be sandboxed, the mitigation is to keep
# what it's allowed to run fixed and auditable instead of handing an LLM
# agent free-form unsandboxed Windows execution: this script takes at most
# one argument (a vstest test-case filter) and runs exactly one known
# procedure — never an interpolated/eval'd command.
#
# Runs against a DEDICATED clone (E:\ci\EEmovel-Backend), never the E:\dev
# one used for manual work in Visual Studio — avoids the automated runner and
# a human stomping on the same working tree (git reset/Web.config/TestResults)
# at the same time.
#
# Full procedure + gotchas documented in ai-memory:
# procedural/eemovel-backend-build-test-windows-interop.md (Playground project)
set -euo pipefail

CI_DIR_WSL="/mnt/e/ci/EEmovel-Backend"
CI_DIR_WIN='E:\ci\EEmovel-Backend'
MSBUILD='C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\amd64\MSBuild.exe'
VSTEST='C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\IDE\CommonExtensions\Microsoft\TestWindow\vstest.console.exe'
TEST_DLL='E:\ci\EEmovel-Backend\EEmovel.Unit.Tests\bin\Debug\net481\EEmovel.Unit.Tests.dll'

# Only variable input accepted: an optional vstest /TestCaseFilter value.
# Never shell-interpolated into anything *this script* executes directly -
# but it does end up as literal text inside a .bat that cmd.exe parses, and
# cmd.exe treats &, |, >, <, ^, %, " as special even inside a quoted argument
# in several contexts. vstest's own filter syntax legitimately overlaps with
# some of those (e.g. Category!=Slow), so this is a real injection surface,
# not a theoretical one - allowlist to a conservative safe subset (name/value
# matching via ~ and =) rather than trying to get cmd.exe quoting exactly
# right. Reject loudly instead of silently stripping.
TEST_FILTER="${1:-}"
if [ -n "$TEST_FILTER" ] && ! [[ "$TEST_FILTER" =~ ^[A-Za-z0-9_.~=]+$ ]]; then
    echo "[error] test filter contains disallowed characters (only letters, digits, . _ ~ = are accepted): $TEST_FILTER"
    exit 4
fi

if [ ! -d "$CI_DIR_WSL" ]; then
    echo "[error] CI clone not found at $CI_DIR_WSL - run the one-time setup"
    echo "        documented in ai-memory procedural/eemovel-backend-build-test-windows-interop.md"
    exit 2
fi

BUILD_BAT="$CI_DIR_WSL/_eemovel_build.bat"
TEST_BAT="$CI_DIR_WSL/_eemovel_test.bat"

cat > "$BUILD_BAT" <<EOF
@echo off
cd /d $CI_DIR_WIN
"$MSBUILD" EEmovelDev.sln /restore /p:Configuration=Debug /m /nologo > _eemovel_build.log 2>&1
echo EXITCODE=%ERRORLEVEL% >> _eemovel_build.log
EOF

if [ -n "$TEST_FILTER" ]; then
    FILTER_ARG="/TestCaseFilter:$TEST_FILTER"
else
    FILTER_ARG=""
fi

cat > "$TEST_BAT" <<EOF
@echo off
cd /d $CI_DIR_WIN
"$VSTEST" "$TEST_DLL" $FILTER_ARG /Logger:trx > _eemovel_test.log 2>&1
echo EXITCODE=%ERRORLEVEL% >> _eemovel_test.log
EOF

echo "[run-eemovel-tests] building (E:\\ci\\EEmovel-Backend)..."
# cmd.exe /c's own stdout is just the ignorable WSL-UNC-cwd warning - the
# real result always lives in the .log file on disk, never trust this stdout.
cmd.exe /c "${CI_DIR_WIN}\\_eemovel_build.bat" >/dev/null 2>&1 || true

BUILD_LOG="$CI_DIR_WSL/_eemovel_build.log"
if [ ! -f "$BUILD_LOG" ]; then
    echo "[error] build did not produce a log file - interop likely failed"
    exit 3
fi

BUILD_TAIL=$(tail -n 6 "$BUILD_LOG")
echo "$BUILD_TAIL"

if ! tr -d '\r' < "$BUILD_LOG" | grep -qE "^EXITCODE=0[[:space:]]*$"; then
    echo "[run-eemovel-tests] build failed, skipping tests"
    exit 1
fi

echo "[run-eemovel-tests] running tests..."
cmd.exe /c "${CI_DIR_WIN}\\_eemovel_test.bat" >/dev/null 2>&1 || true

TEST_LOG="$CI_DIR_WSL/_eemovel_test.log"
if [ ! -f "$TEST_LOG" ]; then
    echo "[error] test run did not produce a log file - interop likely failed"
    exit 3
fi

# Report the real summary block, not cmd.exe's own stdout.
grep -A4 "Total de testes:" "$TEST_LOG" || tail -n 15 "$TEST_LOG"
echo "[run-eemovel-tests] full logs: $BUILD_LOG , $TEST_LOG"
