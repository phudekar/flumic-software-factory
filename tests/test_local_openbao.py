"""Local bootstrap contracts; optional real persistent server qualification."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('local_openbao', ROOT / 'scripts/local-openbao.py')
local = importlib.util.module_from_spec(spec)
spec.loader.exec_module(local)


class LocalOpenBaoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'repo'
        self.root.mkdir()
        self.home = Path(self.temporary.name) / 'home'
        self.home.mkdir()
        self.patch_root = patch.object(local, 'ROOT', self.root)
        self.patch_home = patch.object(Path, 'home', return_value=self.home)
        self.patch_root.start()
        self.patch_home.start()
        self.addCleanup(self.patch_root.stop)
        self.addCleanup(self.patch_home.stop)

    def test_preparation_is_private_persistent_and_preserves_control_keys(self):
        if not shutil.which('openssl'):
            self.skipTest('openssl required')
        original = 'CONTROL_ENCRYPTION_KEY=keep-this-key\nCONTROL_ADMIN_PASSWORD=keep-admin\n'
        (self.root / '.env.control-plane').write_text(original)
        local.prepare()
        directory = local.local_dir()
        certificate = (directory / 'tls/server.pem').read_bytes()
        local.prepare()
        self.assertEqual((directory / 'tls/server.pem').read_bytes(), certificate)
        self.assertEqual((self.root / '.env.control-plane').read_text(), original)
        self.assertFalse(directory.is_relative_to(self.root))
        self.assertEqual((directory / 'tls/server-key.pem').stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / '.env.openbao').stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.root / '.local/openbao/enabled').exists())

    def test_remote_configuration_is_not_overwritten(self):
        (self.root / '.env.control-plane').write_text('CONTROL_OPENBAO_ADDR=https://remote.example\n')
        with self.assertRaisesRegex(RuntimeError, 'refusing'):
            local.prepare()
        self.assertFalse((self.root / '.env.openbao').exists())

    def test_init_control_is_repeatable(self):
        local.init_control()
        original = (self.root / '.env.control-plane').read_bytes()
        local.init_control()
        self.assertEqual((self.root / '.env.control-plane').read_bytes(), original)
        self.assertEqual((self.root / '.env.control-plane').stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(os.environ.get('OPENBAO_TEST_BIN'), 'opt-in real OpenBao binary')
    def test_real_raft_init_unseal_policy_token_renewal_and_restart(self):
        local.prepare()
        directory = local.local_dir()
        config = (directory / 'config/server.hcl').read_text().replace('/openbao/tls', str(directory / 'tls')).replace('/openbao/state', str(directory / 'state')).replace('factory-openbao', 'localhost')
        (directory / 'native.hcl').write_text(config)
        def start():
            log = open(directory / 'native.log', 'ab')
            self.addCleanup(log.close)
            process = subprocess.Popen([os.environ['OPENBAO_TEST_BIN'], 'server', '-config=' + str(directory / 'native.hcl')], stdout=log, stderr=log)
            self.addCleanup(lambda: stop(process))
            client = local.Client(directory)
            for _ in range(100):
                try:
                    client.call('sys/init')
                    return process, client
                except (OSError, RuntimeError):
                    if process.poll() is not None:
                        self.fail('native OpenBao failed to start')
                    time.sleep(.1)
            self.fail('native OpenBao startup deadline')
        def stop(process):
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        process, client = start()
        local.configure(client, directory)
        token = (directory / 'service/token').read_text().strip()
        path = 'flumic/data/projects/00000000-0000-0000-0000-000000000001/credentials/00000000-0000-0000-0000-000000000002'
        client.call(path, {'data': {'value': 'fixture-only'}, 'options': {'cas': 0}}, token)
        local.configure(client, directory)
        self.assertEqual((directory / 'service/token').read_text().strip(), token)
        self.assertEqual(client.call(path, token=token)['data']['data']['value'], 'fixture-only')
        with self.assertRaisesRegex(RuntimeError, 'HTTP 403'):
            client.call('sys/policies/acl/forbidden', {'policy': ''}, token, 'PUT')
        root_token = json.loads((directory / 'bootstrap.json').read_text())['root_token']
        client.call('auth/token/revoke', {'token': token}, root_token)
        local.configure(client, directory)
        replacement = (directory / 'service/token').read_text().strip()
        self.assertNotEqual(token, replacement)
        token = replacement
        stop(process)
        process, client = start()
        self.assertTrue(client.call('sys/seal-status')['sealed'])
        local.configure(client, directory)
        self.assertFalse(client.call('sys/seal-status')['sealed'])
        self.assertEqual(client.call(path, token=token)['data']['data']['value'], 'fixture-only')
        settings = local.env_values(self.root / '.env.control-plane')
        self.assertEqual(settings['CONTROL_OPENBAO_ADDR'], local.ADDRESS)
        self.assertTrue((self.root / '.local/openbao/enabled').exists())
        self.assertNotIn(token, (self.root / '.env.control-plane').read_text())
