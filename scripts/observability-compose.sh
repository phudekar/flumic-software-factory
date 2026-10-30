#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ ! -f .env.observability ]; then
    echo 'Run scripts/start-observability.sh first to initialize the monitoring configuration.' >&2
    exit 1
fi
set -- --env-file .env.observability -f observability/pours/deployment/compose.yaml "$@"
if [ -f .env ]; then set -- --env-file .env "$@"; fi
exec ./scripts/compose-command.sh "$@"
