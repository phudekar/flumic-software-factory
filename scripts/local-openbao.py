#!/usr/bin/env python3
"""Bootstrap the optional local TLS OpenBao. Never print credentials."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
ADDRESS = 'https://factory-openbao:8200'


def private_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(text)
    os.replace(temporary, path)


def env_values(path):
    if not path.exists():
        return {}
    return dict(line.split('=', 1) for line in path.read_text().splitlines()
                if line and not line.startswith('#') and '=' in line)


def init_control():
    path = ROOT / '.env.control-plane'
    if not path.exists():
        private_write(path, '# Private local Control Pane credentials. Back up the encryption key.\n' +
                      '\n'.join(f'{k}={v}' for k, v in {
                          'CONTROL_ADMIN_PASSWORD': secrets.token_urlsafe(32),
                          'CONTROL_DB_PASSWORD': secrets.token_urlsafe(32),
                          'CONTROL_ENCRYPTION_KEY': base64.urlsafe_b64encode(os.urandom(32)).decode(),
                      }.items()) + '\n')


def local_dir():
    values = env_values(ROOT / '.env.openbao')
    if 'OPENBAO_LOCAL_DIR' in values:
        return Path(values['OPENBAO_LOCAL_DIR'])
    identity = hashlib.sha256(str(ROOT).encode()).hexdigest()[:12]
    return Path.home() / '.local/share/flumic-openbao' / identity


def prepare():
    settings = env_values(ROOT / '.env.control-plane')
    if settings.get('CONTROL_OPENBAO_ADDR', '') not in ('', ADDRESS):
        raise RuntimeError('Existing remote OpenBao configuration: refusing to replace it.')
    directory = local_dir()
    if any(c in str(directory) for c in '\n\r$\"\' '):
        raise RuntimeError('Local state path must not contain whitespace or environment interpolation characters.')
    for name in ('config', 'tls', 'state', 'service'):
        (directory / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    certificate = directory / 'tls/server.pem'
    key = directory / 'tls/server-key.pem'
    if certificate.exists() != key.exists():
        raise RuntimeError('Incomplete TLS material; restore the matching certificate and key.')
    if not certificate.exists():
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:3072', '-nodes',
                        '-keyout', str(key), '-out', str(certificate), '-days', '3650',
                        '-subj', '/CN=factory-openbao', '-addext',
                        'subjectAltName=DNS:factory-openbao,DNS:localhost,IP:127.0.0.1',
                        '-addext', 'basicConstraints=critical,CA:TRUE'],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        key.chmod(0o600)
    private_write(directory / 'config/server.hcl', '''ui = true
disable_mlock = true
api_addr = "https://factory-openbao:8200"
cluster_addr = "https://factory-openbao:8201"
storage "raft" {
  path = "/openbao/state"
  node_id = "flumic-local"
}
listener "tcp" {
  address = "0.0.0.0:8200"
  cluster_address = "0.0.0.0:8201"
  tls_cert_file = "/openbao/tls/server.pem"
  tls_key_file = "/openbao/tls/server-key.pem"
}
''')
    private_write(ROOT / '.env.openbao', f'OPENBAO_LOCAL_DIR={directory}\nOPENBAO_LOCAL_UID={os.getuid()}\nOPENBAO_LOCAL_GID={os.getgid()}\n')
    public = ROOT / '.local/openbao/ca.pem'
    private_write(public, certificate.read_text())
    public.chmod(0o644)


class ApiError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f'OpenBao request failed (HTTP {status}); no credentials were logged.')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self, directory):
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(directory / 'tls/server.pem'))))

    def call(self, path, data=None, token=None, method=None):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['X-Vault-Token'] = token
        request = urllib.request.Request('https://localhost:8200/v1/' + path,
                                         data=json.dumps(data).encode() if data is not None else None,
                                         headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=30) as response:
                body = response.read(131072)
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            raise ApiError(code) from None


def configure(client, directory):
    state = client.call('sys/init')
    recovery = directory / 'bootstrap.json'
    if not state['initialized']:
        if recovery.exists():
            raise RuntimeError('Recovery material exists but server is uninitialized; restore its matching state.')
        result = client.call('sys/init', {'secret_shares': 1, 'secret_threshold': 1})
        private_write(recovery, json.dumps(result))
    if not recovery.exists():
        raise RuntimeError('Initialized server has no local recovery file. Restore it; state will not be reset.')
    bootstrap = json.loads(recovery.read_text())
    if client.call('sys/seal-status')['sealed']:
        client.call('sys/unseal', {'key': bootstrap['keys_base64'][0]})
    root_token = bootstrap['root_token']
    # Unseal can return before Raft has elected this node and enabled writes.
    for attempt in range(60):
        leader = client.call('sys/leader')
        if leader.get('is_self') or not leader.get('ha_enabled', True):
            break
        if attempt == 59:
            raise RuntimeError('OpenBao is unsealed but did not become the active node.')
        time.sleep(.5)
    mounts = client.call('sys/mounts', token=root_token)
    if 'flumic/' not in mounts:
        client.call('sys/mounts/flumic', {'type': 'kv', 'options': {'version': '2'}}, root_token)
    elif mounts['flumic/'].get('options', {}).get('version') != '2':
        raise RuntimeError('Existing flumic mount is not KV v2.')
    policy = 'path "flumic/data/projects/*" { capabilities = ["create", "read"] }\n'
    client.call('sys/policies/acl/flumic-control', {'policy': policy}, root_token, 'PUT')
    token_path = directory / 'service/token'
    needs_token = not token_path.exists()
    if not needs_token:
        try:
            client.call('auth/token/renew-self', {}, token_path.read_text().strip())
        except ApiError as error:
            if error.status != 403:
                raise
            needs_token = True  # Expired after downtime: replace the token sink atomically.
    if needs_token:
        result = client.call('auth/token/create', {'policies': ['flumic-control', 'default'],
                             'period': '24h', 'no_parent': True, 'display_name': 'flumic-local-control'}, root_token)
        private_write(token_path, result['auth']['client_token'] + '\n')
    init_control()
    path = ROOT / '.env.control-plane'
    settings = env_values(path)
    if settings.get('CONTROL_OPENBAO_ADDR', '') not in ('', ADDRESS):
        raise RuntimeError('Existing remote OpenBao configuration: refusing to replace it.')
    updates = {'CONTROL_OPENBAO_ADDR': ADDRESS, 'CONTROL_OPENBAO_MOUNT': 'flumic',
               'CONTROL_OPENBAO_TOKEN_FILE': '/run/flumic-openbao/token',
               'CONTROL_OPENBAO_CA_FILE': '/factory/.local/openbao/ca.pem'}
    lines = [line for line in path.read_text().splitlines() if line.split('=', 1)[0] not in updates]
    private_write(path, '\n'.join(lines) + '\n' + ''.join(f'{k}={v}\n' for k, v in updates.items()))
    private_write(ROOT / '.local/openbao/enabled', 'Local OpenBao Compose overlay enabled.\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'setup', 'status', 'init-control'])
    command = parser.parse_args().command
    try:
        if command == 'init-control':
            init_control()
        elif command == 'prepare':
            prepare()
        else:
            directory = local_dir()
            client = Client(directory)
            if command == 'status':
                result = client.call('sys/seal-status')
                print(f"OpenBao initialized={result['initialized']} sealed={result['sealed']}")
            else:
                for attempt in range(30):
                    try:
                        client.call('sys/init')
                        break
                    except (OSError, RuntimeError):
                        if attempt == 29:
                            raise RuntimeError('OpenBao did not become reachable on localhost:8200.') from None
                        time.sleep(1)
                configure(client, directory)
                print('OpenBao ready. Run just control-up to apply the connection settings.')
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
