#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
python3 scripts/local-openbao.py prepare
docker network inspect worker-network >/dev/null 2>&1 || docker network create worker-network >/dev/null
./scripts/openbao-compose.sh up -d
python3 scripts/local-openbao.py setup
./scripts/openbao-compose.sh --profile renewal up -d renew
