#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
python3 scripts/local-openbao.py init-control
if command -v hatchet >/dev/null 2>&1; then
    env_block=$(hatchet profile env --name development) || exit $?
    eval "$env_block"
fi
export FACTORY_HOST_ROOT="$PWD"
mkdir -p data/control-plane/snapshots
docker network inspect worker-network >/dev/null 2>&1 || docker network create worker-network >/dev/null
./scripts/control-compose.sh up --build -d "$@"
printf '%s\n' 'Control plane: http://localhost:3400' 'Sign in with CONTROL_ADMIN_PASSWORD from .env.control-plane (private, ignored by Git).' 'Existing workers were not restarted.'
