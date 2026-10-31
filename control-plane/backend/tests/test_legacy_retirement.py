"""Retirement inventory never infers execution ownership from project alone."""
import unittest
from sqlalchemy import MetaData
from app import database as m
from app.legacy_retirement import inspect_retirement


class RetirementInventoryTests(unittest.TestCase):
    def setUp(self):
        self.engine, self.sessions = m.database('sqlite://')
        self.addCleanup(self.engine.dispose)
        metadata = MetaData()
        for table in m.Base.metadata.tables.values():
            table.to_metadata(metadata)
        metadata.create_all(self.engine)
        with self.sessions.begin() as db:
            db.add(m.Project(id=m.DEFAULT_PROJECT_ID, slug='default', name='Default'))
            db.flush()
            for identity, protocol in [('old', 'legacy'), ('current', 'shared-v1')]:
                db.add(m.Worker(id=identity, name=identity, protocol=protocol,
                    desired_state='stopped', profile='simple', revision_id='revision',
                    credential_hash='private'))
                db.add(m.Event(id=identity, kind='log', worker_id=identity,
                    project_id=m.DEFAULT_PROJECT_ID, occurred_at=1, payload={}))
            db.flush()
            db.add(m.Conversation(id='old', ciphertext='private', expires_at=1))

    def test_inventory_selects_retired_provenance_within_same_project(self):
        with self.sessions() as db:
            result = inspect_retirement(db)
        self.assertEqual(result['worker_ids'], ['old'])
        self.assertEqual(result['event_ids'], ['old'])
        self.assertEqual(result['conversation_ids'], ['old'])
        self.assertNotIn('private', str(result))
        with self.sessions() as db:
            self.assertIsNotNone(db.get(m.Event, 'current'))
            self.assertIsNotNone(db.get(m.Worker, 'old'))

    def test_running_legacy_worker_blocks_retirement(self):
        with self.sessions.begin() as db:
            db.get(m.Worker, 'old').desired_state = 'running'
        with self.sessions() as db:
            with self.assertRaisesRegex(ValueError, 'stopped or removed'):
                inspect_retirement(db)

    def test_project_attribution_on_legacy_event_blocks_retirement(self):
        with self.sessions.begin() as db:
            db.get(m.Event, 'old').execution_id = 'retained-execution'
        with self.sessions() as db:
            with self.assertRaisesRegex(ValueError, 'execution attribution'):
                inspect_retirement(db)

    def test_pending_lifecycle_operation_blocks_retirement(self):
        with self.sessions.begin() as db:
            db.add(m.Operation(id='op', worker_id='old', action='start',
                               status='pending', key='old-start'))
        with self.sessions() as db:
            with self.assertRaisesRegex(ValueError, 'pending lifecycle'):
                inspect_retirement(db)

    def test_retained_runtime_binding_blocks_even_stopped_worker(self):
        with self.sessions.begin() as db:
            db.add(m.RuntimeResourceBinding(id='binding', target_id='kube',
                target_revision='revision', kind='Deployment', logical_id='worker',
                namespace='flumic', name='worker', uid='remote',
                spec_digest='digest', worker_id='old'))
        with self.sessions() as db:
            with self.assertRaisesRegex(ValueError, 'retained runtime data'):
                inspect_retirement(db)

    def test_unmatched_conversations_are_preserved(self):
        with self.sessions.begin() as db:
            db.add(m.Conversation(id='unknown', ciphertext='private', expires_at=1))
            db.add(m.Conversation(id='current', ciphertext='private', expires_at=1))
        with self.sessions() as db:
            self.assertEqual(inspect_retirement(db)['conversation_ids'], ['old'])

    def test_manifest_changes_when_target_content_changes(self):
        with self.sessions() as db:
            before = inspect_retirement(db)
        with self.sessions.begin() as db:
            db.get(m.Event, 'old').payload = {'message': 'changed'}
        with self.sessions() as db:
            after = inspect_retirement(db)
        self.assertNotEqual(before['fingerprint'], after['fingerprint'])
        self.assertNotIn('changed', str(after))

    def test_manifest_is_stable_without_changes(self):
        with self.sessions() as db:
            before = inspect_retirement(db)
        with self.sessions() as db:
            self.assertEqual(before, inspect_retirement(db))

    def test_assignment_attribution_alone_blocks_retirement(self):
        with self.sessions.begin() as db:
            db.get(m.Event, 'old').assignment_id = 'assignment'
        with self.sessions() as db:
            with self.assertRaisesRegex(ValueError, 'execution attribution'):
                inspect_retirement(db)

    def test_dry_run_command_writes_private_manifest_and_no_data(self):
        import json
        import os
        from pathlib import Path
        import subprocess
        import sys
        import tempfile
        root = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / 'fixture.sqlite'
            with self.engine.connect() as source:
                import sqlite3
                target = sqlite3.connect(database_path)
                source.connection.driver_connection.backup(target)
                target.close()
            output = Path(directory) / 'manifest.json'
            result = subprocess.run(
                [sys.executable, str(root / 'scripts/retire-legacy-runtime.py'),
                 '--output', str(output)],
                env={**os.environ, 'CONTROL_DATABASE_URL': 'sqlite:///' + str(database_path)},
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(output.read_text())['worker_ids'], ['old'])
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('private', result.stdout + result.stderr)
            self.assertFalse('old' in result.stdout)
            connection = sqlite3.connect(database_path)
            self.assertEqual(connection.execute('SELECT count(*) FROM worker_instances').fetchone()[0], 2)
            connection.close()

    def test_dry_run_connection_errors_do_not_echo_credentials(self):
        import os
        from pathlib import Path
        import subprocess
        import sys
        import tempfile
        root = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, str(root / 'scripts/retire-legacy-runtime.py'),
                 '--output', str(Path(directory) / 'manifest.json')],
                env={**os.environ, 'CONTROL_DATABASE_URL': 'unknown://user:very-private-password@host/db'},
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertNotIn('very-private-password', result.stdout + result.stderr)
            self.assertFalse((Path(directory) / 'manifest.json').exists())
