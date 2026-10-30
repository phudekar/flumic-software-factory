#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
env_block=$(hatchet profile env --name "development") || exit $?
eval "$env_block"
: "${HATCHET_CLIENT_TOKEN:?The development profile did not export HATCHET_CLIENT_TOKEN}"
exec docker-compose -f compose.yaml -f compose.workers-observability.yaml up --build "$@"
