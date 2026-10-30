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
