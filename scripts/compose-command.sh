#!/bin/sh
# Support both common installations of Docker Compose v2.
set -eu
if command -v docker-compose >/dev/null 2>&1; then
    exec docker-compose "$@"
fi
exec docker compose "$@"
