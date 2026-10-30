#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ ! -f .env.control-plane ]; then
    python3 - <<'PY'
import base64, os, secrets
from pathlib import Path
values = {
    'CONTROL_ADMIN_PASSWORD': secrets.token_urlsafe(32),
    'CONTROL_DB_PASSWORD': secrets.token_urlsafe(32),
    'CONTROL_ENCRYPTION_KEY': base64.urlsafe_b64encode(os.urandom(32)).decode(),
}
p = Path('.env.control-plane')
fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd,'w') as output:
    output.write('# Private local control-plane credentials. Back up the encryption key.\n')
    for name,value in values.items(): output.write(name+'='+value+'\n')
PY
fi
if command -v hatchet >/dev/null 2>&1; then
    env_block=$(hatchet profile env --name development) || exit $?
    eval "$env_block"
fi
export FACTORY_HOST_ROOT="$PWD"
mkdir -p data/control-plane/snapshots
docker network inspect worker-network >/dev/null 2>&1 || docker network create worker-network >/dev/null
./scripts/control-compose.sh up --build -d "$@"
printf '%s\n' 'Control plane: http://localhost:3400' 'Sign in with CONTROL_ADMIN_PASSWORD from .env.control-plane (private, ignored by Git).' 'Existing workers were not restarted.'
