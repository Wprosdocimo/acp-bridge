#!/bin/bash
# Build harness-factory as static binary for Lambda Layer
# Run this in the ACP-Harness-Factory source directory.
#
# Usage:
#   cd /path/to/ACP-Harness-Factory
#   bash /path/to/acp-bridge/infra/lambda-burst/layer/build.sh
#
# Output: ./harness-factory-lambda (static, ~6MB stripped)

set -euo pipefail

echo "=== Building harness-factory for Lambda (linux/amd64, static) ==="

CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
  go build -ldflags="-s -w" -o harness-factory-lambda ./cmd/harness-factory

ls -lh harness-factory-lambda
file harness-factory-lambda

echo ""
echo "✅ Build complete. Next steps:"
echo "   cp harness-factory-lambda /path/to/acp-bridge/infra/lambda-burst/layer/"
echo "   cd /path/to/acp-bridge/infra/lambda-burst/cdk && cdk deploy"
