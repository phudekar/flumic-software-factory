#!/bin/sh
# Select deployment configuration without starting or rebuilding anything.
set -eu
cd "$(dirname "$0")/.."
export FACTORY_HOST_ROOT="$PWD"
case "${CONTROL_DATABASE_MODE:-local}" in
    local) set -- -f compose.control-plane.yaml -f compose.control-db.yaml "$@" ;;
    external) set -- -f compose.control-plane.yaml "$@" ;;
    *) echo 'CONTROL_DATABASE_MODE must be local or external.' >&2; exit 1 ;;
esac
if [ ! -f .env.control-plane ]; then
    echo 'Run scripts/start-control-plane.sh first to initialize private credentials.' >&2
    exit 1
fi
set -- --env-file .env.control-plane "$@"
if [ -f .env ]; then set -- --env-file .env "$@"; fi
# Build the rendering command separately: requested lifecycle arguments may be
# arbitrary and must never be interpreted as configuration options.
if [ "${CONTROL_DATABASE_MODE:-local}" = external ]; then
    if [ -f .env ]; then
        ./scripts/compose-command.sh --env-file .env --env-file .env.control-plane -f compose.control-plane.yaml config --format json
    else
        ./scripts/compose-command.sh --env-file .env.control-plane -f compose.control-plane.yaml config --format json
    fi | python3 -c '
import json, sys
from urllib.parse import urlsplit
model = json.load(sys.stdin)
url = model["services"]["migrate"]["environment"].get("CONTROL_DATABASE_URL", "")
try:
    parsed = urlsplit(url)
    valid = parsed.scheme == "postgresql+psycopg" and parsed.hostname and parsed.path.strip("/")
except ValueError:
    valid = False
if not valid:
    sys.exit("External mode requires CONTROL_DATABASE_URL=postgresql+psycopg://... with a host and database.")
'
fi
exec ./scripts/compose-command.sh "$@"
