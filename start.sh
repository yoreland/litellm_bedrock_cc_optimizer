#!/bin/bash
# Start LiteLLM Proxy with Bedrock Optimizer
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Activate venv
source .venv/bin/activate

# Ensure bedrock_optimizer is importable
export PYTHONPATH="$SCRIPT_DIR:$PYTHONPATH"

echo "Starting LiteLLM Proxy with Bedrock Optimizer..."
echo "  Config: config.yaml"
echo "  Port:   4000"
echo "  Cache:  ${CACHE_ENABLED:-1} (TTL: ${CACHE_TTL:-1h})"
echo ""

exec litellm --config config.yaml --port 4000 "$@"
