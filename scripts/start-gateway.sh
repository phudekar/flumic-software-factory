#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ ! -f .env.litellm ]; then
    python3 - <<'PY'
import os
from pathlib import Path
import secrets
values = {
    'LITELLM_DB_PASSWORD': secrets.token_hex(32),
    'LITELLM_MASTER_KEY': 'sk-' + secrets.token_hex(32),
    'LITELLM_SALT_KEY': 'sk-' + secrets.token_hex(32),
}
fd = os.open(Path('.env.litellm'), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as output:
    output.write('# Private gateway credentials. Preserve the salt with database backups.\n')
    for key, value in values.items():
        output.write(key + '=' + value + '\n')
PY
fi
docker network inspect worker-network >/dev/null 2>&1 || docker network create worker-network >/dev/null
./scripts/gateway-compose.sh up -d "$@"
printf '%s\n' 'LiteLLM UI: http://localhost:4000/ui' 'Admin credentials: .env.litellm (private). Flumic model routing is unchanged.'
