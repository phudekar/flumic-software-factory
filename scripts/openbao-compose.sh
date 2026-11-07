#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ ! -f .env.openbao ]; then
    echo 'Run just openbao-up first.' >&2
    exit 1
fi
exec ./scripts/compose-command.sh --env-file .env.openbao -f compose.openbao.yaml "$@"
