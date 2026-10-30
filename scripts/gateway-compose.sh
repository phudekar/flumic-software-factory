#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ ! -f .env.litellm ]; then
    echo 'Run scripts/start-gateway.sh first to initialize private gateway credentials.' >&2
    exit 1
fi
exec ./scripts/compose-command.sh --env-file .env.litellm -f compose.litellm.yaml "$@"
