"""Deployment contracts: real Compose rendering plus isolated shell execution.

Fixtures never start Docker workloads or read the operator's private env files.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ([shutil.which('docker-compose')] if shutil.which('docker-compose') else
           [shutil.which('docker'), 'compose'] if shutil.which('docker') else None)


class ComposeRenderingTests(unittest.TestCase):
    def render(self, *files, extra=None, profiles=()):
        if not COMPOSE:
            self.skipTest('Docker Compose is required for manifest validation')
        env = {
            'PATH': os.environ['PATH'], 'HOME': os.environ.get('HOME', '/tmp'),
            'CONTROL_DB_PASSWORD': 'fixture-password',
            'CONTROL_ADMIN_PASSWORD': 'fixture-admin',
            'CONTROL_ENCRYPTION_KEY': 'fixture-encryption',
            'FACTORY_HOST_ROOT': '/fixture',
            'LITELLM_DB_PASSWORD': 'fixture-gateway-password',
            'LITELLM_MASTER_KEY': 'sk-fixture-admin',
            'LITELLM_SALT_KEY': 'sk-fixture-salt',
            **(extra or {}),
        }
        with tempfile.NamedTemporaryFile() as empty_env:
            args = [*COMPOSE, '--env-file', empty_env.name]
            for profile in profiles:
                args += ['--profile', profile]
            for name in files:
                args += ['-f', str(ROOT / name)]
            result = subprocess.run([*args, 'config', '--format', 'json'],
                                    env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_local_database_keeps_existing_identity_and_gates_migrations(self):
        model = self.render('compose.control-plane.yaml', 'compose.control-db.yaml')
        self.assertEqual(model['name'], 'factory-control')
        self.assertEqual(model['volumes']['control-db']['name'], 'factory-control_control-db')
        services = model['services']
        self.assertEqual(services['migrate']['command'], ['alembic', 'upgrade', 'head'])
        self.assertEqual(services['migrate']['depends_on']['postgres']['condition'], 'service_healthy')
        self.assertEqual(services['migrate']['restart'], 'no')
        for name in ('api', 'reconciler', 'dynamic-runtime'):
            self.assertEqual(services[name]['depends_on']['migrate']['condition'],
                             'service_completed_successfully')
            self.assertEqual(services[name]['environment']['CONTROL_DATABASE_URL'],
                             services['migrate']['environment']['CONTROL_DATABASE_URL'])
        self.assertNotIn('alembic', ' '.join(services['api']['command']))
        self.assertNotIn('CONTROL_ENCRYPTION_KEY', services['migrate']['environment'])

    def test_external_database_uses_operator_url_without_local_postgres(self):
        url = 'postgresql+psycopg://test:fixture@external-db:5432/control'
        model = self.render('compose.control-plane.yaml', extra={'CONTROL_DATABASE_URL': url})
        self.assertNotIn('postgres', model['services'])
        for service in model['services'].values():
            self.assertEqual(service['environment']['CONTROL_DATABASE_URL'], url)
        self.assertNotIn('postgres', model['services']['migrate'].get('depends_on', {}))

    def test_existing_host_database_path_is_preserved(self):
        model = self.render('compose.control-plane.yaml', 'compose.control-db.yaml',
                            extra={'CONTROL_DB_DATA_PATH': '/fixture/existing-postgres'})
        self.assertEqual(model['services']['postgres']['volumes'][0]['source'],
                         '/fixture/existing-postgres')

    def test_gateway_has_independent_private_database_and_loopback_port(self):
        model = self.render('compose.litellm.yaml')
        self.assertEqual(model['name'], 'flumic-gateway')
        services = model['services']
        self.assertEqual(set(services), {'litellm', 'litellm-db'})
        self.assertEqual(services['litellm']['ports'][0]['host_ip'], '127.0.0.1')
        self.assertNotIn('ports', services['litellm-db'])
        self.assertEqual(set(services['litellm-db']['networks']), {'private'})
        self.assertTrue(model['networks']['private']['internal'])
        self.assertEqual(model['networks']['workers']['name'], 'worker-network')
        self.assertEqual(services['litellm']['environment']['STORE_MODEL_IN_DB'], 'True')
        self.assertNotIn('main-stable', services['litellm']['image'])
        self.assertNotIn('OPENAI_API_KEY', services['litellm-db']['environment'])

    def test_observability_provisioning_is_an_explicit_profile(self):
        default = self.render('observability/pours/deployment/compose.yaml')
        self.assertNotIn('dashboard-bootstrap', default['services'])
        model = self.render('observability/pours/deployment/compose.yaml', profiles=('provisioning',))
        self.assertEqual(model['services']['dashboard-bootstrap']['profiles'], ['provisioning'])

    def test_nonstart_control_commands_resolve_host_root_and_private_env(self):
        if not COMPOSE:
            self.skipTest('Docker Compose is required')
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            shutil.copytree(ROOT / 'scripts', target / 'scripts')
            for name in ('compose.control-plane.yaml', 'compose.control-db.yaml'):
                shutil.copy2(ROOT / name, target / name)
            (target / '.env.control-plane').write_text(
                'CONTROL_ADMIN_PASSWORD=fixture\nCONTROL_ENCRYPTION_KEY=fixture\nCONTROL_DB_PASSWORD=fixture\n')
            result = subprocess.run(['sh', str(target / 'scripts/control-compose.sh'),
                                     'config', '--format', 'json'],
                                    env={'PATH': os.environ['PATH'], 'HOME': os.environ.get('HOME', '/tmp')},
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            model = json.loads(result.stdout)
            self.assertEqual(Path(model['services']['api']['environment']['FACTORY_HOST_ROOT']).resolve(),
                             target.resolve())



class ShellLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        shutil.copytree(ROOT / 'scripts', self.root / 'scripts')
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.log = self.root / 'commands.jsonl'
        self.env = {**os.environ, 'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                    'FIXTURE_LOG': str(self.log), 'CONTROL_DATABASE_MODE': 'local'}
        for key in ('CONTROL_DATABASE_URL', 'LITELLM_MASTER_KEY', 'LITELLM_SALT_KEY'):
            self.env.pop(key, None)
        self.fake('docker-compose', '''import json, os, sys
with open(os.environ['FIXTURE_LOG'], 'a') as out:
    out.write(json.dumps(sys.argv[1:]) + '\\n')
if 'config' in sys.argv:
    print(json.dumps({'services': {'migrate': {'environment': {
        'CONTROL_DATABASE_URL': os.environ.get('FIXTURE_DATABASE_URL', '')}}}}))
if 'run' in sys.argv and 'dashboard-bootstrap' in sys.argv:
    sys.exit(int(os.environ.get('FIXTURE_PROVISION_EXIT', '0')))
''')
        self.fake('docker', '''import json, os, sys
with open(os.environ['FIXTURE_LOG'], 'a') as out:
    out.write(json.dumps(['docker', *sys.argv[1:]]) + '\\n')
''')
        self.fake('hatchet', 'pass\n')
        (self.root / '.env.control-plane').write_text('CONTROL_ADMIN_PASSWORD=fixture\n')
        (self.root / '.env.observability').write_text('# fixture\n')

    def fake(self, name, body):
        target = self.bin / name
        target.write_text('#!/usr/bin/env python3\n' + body)
        target.chmod(0o755)

    def run_script(self, name, *args, extra=None):
        return subprocess.run(['sh', str(self.root / 'scripts' / name), *args],
                              env={**self.env, **(extra or {})}, text=True,
                              capture_output=True)

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_control_start_selects_local_overlay_and_does_not_build_workers(self):
        result = self.run_script('start-control-plane.sh', 'api')
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.commands()
        self.assertFalse(any('build' in cmd for cmd in commands))
        up = next(cmd for cmd in commands if 'up' in cmd)
        self.assertIn('compose.control-db.yaml', up)
        self.assertEqual(up[-1], 'api')

    def test_external_mode_rejects_missing_url_before_up(self):
        result = self.run_script('start-control-plane.sh', extra={'CONTROL_DATABASE_MODE': 'external'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('CONTROL_DATABASE_URL', result.stderr)
        self.assertFalse(any('up' in cmd for cmd in self.commands()))

    def test_external_mode_forwards_external_url_without_local_overlay(self):
        result = self.run_script('control-compose.sh', 'logs', '--tail', '17', 'api', extra={
            'CONTROL_DATABASE_MODE': 'external',
            'FIXTURE_DATABASE_URL': 'postgresql+psycopg://test:fixture@external-db/control'})
        self.assertEqual(result.returncode, 0, result.stderr)
        command = self.commands()[-1]
        self.assertNotIn('compose.control-db.yaml', command)
        self.assertEqual(command[-4:], ['logs', '--tail', '17', 'api'])
        self.assertNotIn('fixture@external-db', result.stdout + result.stderr)

    def test_invalid_database_mode_fails_before_compose(self):
        result = self.run_script('control-compose.sh', 'up', extra={'CONTROL_DATABASE_MODE': 'typo'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('CONTROL_DATABASE_MODE', result.stderr)
        self.assertFalse(self.commands())

    def test_gateway_bootstraps_private_credentials_once(self):
        result = self.run_script('start-gateway.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.root / '.env.litellm'
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        before = target.read_bytes()
        self.assertIn(b'LITELLM_SALT_KEY=', before)
        self.assertEqual(self.run_script('start-gateway.sh').returncode, 0)
        self.assertEqual(target.read_bytes(), before)
        self.assertNotIn(before.decode().splitlines()[-1], result.stdout + result.stderr)
        self.assertTrue(any('compose.litellm.yaml' in cmd and 'up' in cmd for cmd in self.commands()))

    def test_dashboard_failure_warns_without_failing_monitoring_start(self):
        result = self.run_script('start-observability.sh', extra={'FIXTURE_PROVISION_EXIT': '7'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('provision', result.stderr.lower())
        self.assertFalse((self.root / 'data/observability-bootstrap/.provisioned').exists())
        up = next(cmd for cmd in self.commands() if 'up' in cmd)
        self.assertNotIn('--profile', up)

    def test_successful_bootstrap_runs_once_and_explicit_provision_reruns(self):
        for _ in range(2):
            result = self.run_script('start-observability.sh')
            self.assertEqual(result.returncode, 0, result.stderr)
        provision = lambda: [cmd for cmd in self.commands() if 'run' in cmd and 'dashboard-bootstrap' in cmd]
        self.assertEqual(len(provision()), 1)
        self.assertEqual(self.run_script('provision-dashboards.sh').returncode, 0)
        self.assertEqual(len(provision()), 2)

    def test_monitoring_can_run_without_host_resource_metrics(self):
        result = self.run_script('start-observability.sh', extra={'FLUMIC_RESOURCE_METRICS': '0'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any('compose.resource-metrics.yaml' in cmd for cmd in self.commands()))

    def test_explicit_image_build_never_starts_workers(self):
        result = self.run_script('build-worker-images.sh', '--no-cache')
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.commands()
        builds = [cmd for cmd in commands if cmd[:2] == ['docker', 'build']]
        self.assertEqual(len(builds), 2)
        self.assertEqual({cmd[cmd.index('-t') + 1] for cmd in builds},
                         {'hatchet-test-worker:latest', 'hatchet-test-developer-worker:latest'})
        self.assertTrue(all('--no-cache' in cmd for cmd in builds))
        self.assertFalse(any('up' in cmd for cmd in commands))

    def test_gateway_down_and_logs_never_bootstrap_secrets(self):
        for command in ('down', 'logs'):
            result = self.run_script('gateway-compose.sh', command)
            self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / '.env.litellm').exists())
        self.assertFalse(self.commands())


if __name__ == '__main__':
    unittest.main()
