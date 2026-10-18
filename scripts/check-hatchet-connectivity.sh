#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

env_block=$(hatchet profile env --name "development") || exit $?
eval "$env_block"

: "${HATCHET_CLIENT_TOKEN:?The development profile did not export HATCHET_CLIENT_TOKEN}"

exec docker-compose run --build --rm --no-deps -T \
    --entrypoint python worker -u /worker/check_hatchet_connectivity.py "$@"
