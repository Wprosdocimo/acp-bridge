# Lambda Layer — harness-factory binary

This directory contains the harness-factory binary that gets deployed as a Lambda Layer.

## Structure

After building, this directory should look like:

```
layer/
├── bin/
│   └── harness-factory    ← static Go binary (~6MB)
├── build.sh               ← build script
└── README.md              ← this file
```

Lambda Layer unpacks to `/opt/`, so the binary will be at `/opt/bin/harness-factory`.

## Build

```bash
cd /path/to/ACP-Harness-Factory
bash /path/to/acp-bridge/infra/lambda-burst/layer/build.sh

# Copy output to layer directory
mkdir -p /path/to/acp-bridge/infra/lambda-burst/layer/bin
cp harness-factory-lambda /path/to/acp-bridge/infra/lambda-burst/layer/bin/harness-factory
chmod +x /path/to/acp-bridge/infra/lambda-burst/layer/bin/harness-factory
```

## Requirements

- Go 1.21+
- `CGO_ENABLED=0` (static binary, no glibc dependency)
- Target: `linux/amd64` (Lambda AL2023)

## Binary size

Unstripped: ~10MB
Stripped (`-ldflags="-s -w"`): ~6MB

Lambda Layer size limit: 250MB (unzipped). We're well within budget.

## .gitignore

The `bin/` directory is gitignored — do not commit the binary.
