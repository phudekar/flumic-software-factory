#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
mkdir -p data/control-plane
docker network inspect factory-signoz-network >/dev/null 2>&1 || {
    echo 'Start SigNoz first with scripts/start-observability.sh.' >&2
    exit 1
}
docker-compose -f compose.resource-metrics.yaml up -d "$@"
