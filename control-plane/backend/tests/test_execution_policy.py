"""Versioned execution mode policy; no workers or provider traffic."""
import unittest
from sqlalchemy import select
from fastapi.testclient import TestClient
import test_api as fixtures
from app.database import DEFAULT_PROJECT_ID, Audit


class ExecutionPolicyTests(unittest.TestCase):
    setUp = fixtures.ControlPlaneTests.setUp
    tearDown = fixtures.ControlPlaneTests.tearDown

    @property
    def endpoint(self):
        return f'/api/v1/projects/{DEFAULT_PROJECT_ID}/execution-policy'

    def policy(self):
        response = self.client.get(self.endpoint)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def update(self, policy, **changes):
        return self.client.put(self.endpoint, json={
            'expected_revision': policy['revision_id'],
            'default_mode': changes.get('default_mode', policy['default_mode']),
            'minimum_mode': changes.get('minimum_mode', policy['minimum_mode']),
        })

    def test_existing_project_defaults_direct_without_mutating_configuration(self):
        original = self.client.get('/api/v1/projects/' + DEFAULT_PROJECT_ID).json()
        policy = self.policy()
        self.assertEqual(policy['default_mode'], 'direct')
        self.assertEqual(policy['minimum_mode'], 'direct')
        self.assertTrue(policy['revision_id'])
        self.assertEqual(self.policy()['revision_id'], policy['revision_id'])
        self.assertEqual(self.client.get('/api/v1/projects/' + DEFAULT_PROJECT_ID).json(), original)

    def test_policy_conflict_preserves_new_head_and_old_revision(self):
        from app.database import ExecutionPolicyRevision
        first = self.policy()
        updated = self.update(first, default_mode='protected')
        self.assertEqual(updated.status_code, 200, updated.text)
        second = updated.json()
        self.assertNotEqual(second['revision_id'], first['revision_id'])
        third = self.update(second, minimum_mode='protected')
        self.assertEqual(third.status_code, 200, third.text)
        self.assertEqual(self.update(second, default_mode='direct').status_code, 409)
        self.assertEqual(self.policy()['revision_id'], third.json()['revision_id'])
        with self.sessions() as db:
            old = db.get(ExecutionPolicyRevision, second['revision_id'])
            self.assertEqual(old.default_mode, 'protected')
            self.assertEqual(old.minimum_mode, 'direct')
            audits = list(db.scalars(select(Audit).where(Audit.action == 'execution-policy.update')))
            self.assertEqual(len(audits), 2)

    def test_unchanged_policy_save_does_not_create_history_or_audit(self):
        from app.database import ExecutionPolicyRevision
        first = self.policy()
        unchanged = self.update(first)
        self.assertEqual(unchanged.status_code, 200, unchanged.text)
        self.assertEqual(unchanged.json()['revision_id'], first['revision_id'])
        second = self.update(first, default_mode='protected').json()
        unchanged = self.update(second)
        self.assertEqual(unchanged.status_code, 200, unchanged.text)
        self.assertEqual(unchanged.json()['revision_id'], second['revision_id'])
        with self.sessions() as db:
            self.assertEqual(len(list(db.scalars(select(ExecutionPolicyRevision)))), 1)
            self.assertEqual(len(list(db.scalars(select(Audit).where(Audit.action == 'execution-policy.update')))), 1)

    def test_invalid_mode_and_corrupt_policy_reference_fail_closed(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from fastapi import HTTPException
        from factory.domain.execution import ExecutionPolicy, ExecutionPolicyError
        from app.execution_policy import resolve_execution_pin, read_execution_policy
        from app.projects import ProjectScope
        policy = ExecutionPolicy(revision_id='direct-v1')
        for requested, parent in (('unknown', None), (None, 'unknown')):
            with self.assertRaises(ExecutionPolicyError):
                resolve_execution_pin(policy, requested, parent)
        for revision in (None, SimpleNamespace(project_id='other')):
            db = Mock()
            db.get.side_effect = [SimpleNamespace(revision_id='bad'), revision]
            with self.assertRaises(HTTPException) as error:
                read_execution_policy(db, ProjectScope(DEFAULT_PROJECT_ID))
            self.assertEqual(error.exception.status_code, 503)

    def test_default_cannot_weaken_minimum_and_unknown_modes_rejected(self):
        policy = self.policy()
        for changes in ({'minimum_mode': 'protected'}, {'default_mode': 'proxy'}, {'minimum_mode': 'DIRECT'}):
            with self.subTest(changes=changes):
                self.assertEqual(self.update(policy, **changes).status_code, 422)
                self.assertEqual(self.policy()['revision_id'], policy['revision_id'])

    def test_minimum_protected_rejects_direct_resolution(self):
        from factory.domain.execution import ExecutionPolicy, ExecutionPolicyError
        from app.execution_policy import resolve_execution_pin
        direct = ExecutionPolicy(revision_id='policy', default_mode='direct', minimum_mode='direct')
        self.assertEqual(resolve_execution_pin(direct, None, None).mode, 'direct')
        self.assertEqual(resolve_execution_pin(direct, 'protected', None).mode, 'protected')
        protected = ExecutionPolicy(revision_id='policy', default_mode='protected', minimum_mode='protected')
        for requested, parent in (('direct', None), ('direct', 'protected')):
            with self.assertRaises(ExecutionPolicyError):
                resolve_execution_pin(protected, requested, parent)
        self.assertEqual(resolve_execution_pin(direct, None, 'protected').mode, 'protected')
        with self.assertRaises(ExecutionPolicyError):
            resolve_execution_pin(direct, 'direct', 'protected')

    def test_non_admin_cannot_read_or_weaken_policy(self):
        with TestClient(self.app) as stranger:
            self.assertEqual(stranger.get(self.endpoint).status_code, 401)
            self.assertEqual(stranger.put(self.endpoint, json={}).status_code, 401)
            stranger.headers['Authorization'] = 'Bearer worker-fixture'
            self.assertEqual(stranger.put(self.endpoint, json={}).status_code, 401)
        original_csrf = self.client.headers.pop('X-CSRF-Token')
        try:
            self.assertEqual(self.client.put(self.endpoint, json={}).status_code, 403)
        finally:
            self.client.headers['X-CSRF-Token'] = original_csrf

    def test_foreign_revision_cannot_be_used_as_project_head(self):
        first = self.policy()
        other = self.client.post('/api/v1/projects', json={'slug':'other-policy','name':'Other'}).json()['id']
        path = f'/api/v1/projects/{other}/execution-policy'
        original = self.client.get(path)
        self.assertEqual(original.status_code, 200, original.text)
        updated = self.update(first, default_mode='protected').json()
        response = self.client.put(path, json={'expected_revision': updated['revision_id'],
                                              'default_mode':'protected', 'minimum_mode':'protected'})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.client.get(path).json()['revision_id'], original.json()['revision_id'])
        self.assertEqual(self.client.get('/api/v1/projects/missing/execution-policy').status_code, 404)

    @unittest.skipUnless(__import__('os').environ.get('CONTROL_TEST_DATABASE_URL', '').startswith('postgresql'),
                         'PostgreSQL concurrency contract')
    def test_concurrent_updates_have_exactly_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        first = self.policy()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.update, first, default_mode='protected') for _ in range(2)]
            results = sorted(f.result().status_code for f in futures)
        self.assertEqual(results, [200, 409])
        with self.sessions() as db:
            self.assertEqual(len(list(db.scalars(select(Audit).where(Audit.action == 'execution-policy.update')))), 1)


class PolicyAdmissionGuardTests(unittest.TestCase):
    import test_workflow_state as workflow_fixtures
    setUp = fixtures.ControlPlaneTests.setUp
    tearDown = fixtures.ControlPlaneTests.tearDown
    setup_graph = workflow_fixtures.WorkflowStateTests.setup_graph
    publish = workflow_fixtures.WorkflowStateTests.publish
    worker = workflow_fixtures.WorkflowStateTests.worker
    arrange = workflow_fixtures.WorkflowStateTests.arrange
    start = workflow_fixtures.WorkflowStateTests.start

    def test_unqualified_protected_policy_blocks_new_runs_but_preserves_admitted_run(self):
        from fastapi import HTTPException
        from app.execution_policy import read_execution_policy, update_execution_policy
        scope, repo, definition = self.arrange()
        accepted = self.start(scope, definition)
        with self.sessions.begin() as db:
            policy = read_execution_policy(db, scope)
            update_execution_policy(db, scope, policy.revision_id,
                                    {'default_mode':'protected', 'minimum_mode':'protected'})
        # An idempotent acknowledgement is not a new admission.
        self.assertEqual(self.start(scope, definition).id, accepted.id)
        with self.assertRaises(HTTPException) as error:
            self.start(scope, definition, key='new-protected')
        self.assertEqual(error.exception.status_code, 409)
        self.assertIn('not available', error.exception.detail)
        with self.assertRaises(HTTPException) as error:
            self.store.start_pinned_run(scope, accepted.snapshot_id, {}, key='pinned-protected')
        self.assertEqual(error.exception.status_code, 409)
        policy = self.client.get(f'/api/v1/projects/{scope.project_id}/execution-policy').json()
        self.assertEqual(policy['admitted_direct_runs'], 1)
        self.assertFalse(policy['protected_execution_available'])


class PolicyMigrationTests(unittest.TestCase):
    def test_additive_migration_preserves_legacy_project_and_guards_downgrade(self):
        import os
        import tempfile
        import uuid
        from pathlib import Path
        from unittest.mock import patch
        from alembic import command
        from alembic.config import Config
        from sqlalchemy import create_engine, text, inspect
        from sqlalchemy.engine import make_url
        from app.database import database
        from app.projects import ProjectScope
        from app.execution_policy import read_execution_policy, update_execution_policy
        root = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as temporary:
            url = os.environ.get('CONTROL_MIGRATION_TEST_URL', 'sqlite:///' + temporary + '/migration.db')
            admin = None
            if url.startswith('postgresql'):
                schema = 'policy_migration_' + uuid.uuid4().hex
                admin = create_engine(url)
                with admin.begin() as db:
                    db.execute(text('CREATE SCHEMA "' + schema + '"'))
                url = make_url(url).update_query_dict({'options': '-csearch_path=' + schema}).render_as_string(hide_password=False)
            engine = None
            try:
                config = Config(str(root / 'control-plane/backend/alembic.ini'))
                config.set_main_option('script_location', str(root / 'control-plane/backend/migrations'))
                with patch.dict(os.environ, {'CONTROL_DATABASE_URL': url}):
                    command.upgrade(config, 'e201f1000013')
                    engine, sessions = database(url)
                    before = set(inspect(engine).get_table_names())
                    with engine.begin() as db:
                        db.execute(text("UPDATE projects SET name='Legacy' WHERE id=:id"), {'id': DEFAULT_PROJECT_ID})
                        for statement in (
                            "INSERT INTO repository_connections (id,project_id,alias,provider,host,owner,name,overrides,created_at) VALUES ('old-repo',:id,'old','github','github.com','fixture','repo','{}',1)",
                            "INSERT INTO worker_pools (id,name,profile,created_at) VALUES ('old-pool','old','{}',1)",
                            "INSERT INTO execution_assignments (id,project_id,repository_id,pool_id,run_id,stage_id,execution_id,role,executor,action,status,input_ciphertext,created_at) VALUES ('old-assignment',:id,'old-repo','old-pool','old-run','echo','old-attempt','simple','builtin@1','core.echo@1','completed','opaque-input',1)",
                            "INSERT INTO workflow_definitions (id,project_id,name,created_at) VALUES ('old-definition',:id,'Old workflow',1)",
                            "INSERT INTO workflow_definition_revisions (id,project_id,definition_id,digest,graph_ciphertext,created_at) VALUES ('old-revision',:id,'old-definition','digest','opaque-graph',1)",
                            "INSERT INTO workflow_launch_snapshots (id,project_id,workflow_revision_id,ciphertext,created_at) VALUES ('old-snapshot',:id,'old-revision','opaque-launch',1)",
                            "INSERT INTO workflow_runs (id,project_id,snapshot_id,sequence,request_key,request_digest,input_ciphertext,state,created_at) VALUES ('old-run',:id,'old-snapshot',1,'old-key','digest','opaque-input','completed',1)",
                        ):
                            db.execute(text(statement), {'id': DEFAULT_PROJECT_ID})
                    command.upgrade(config, 'head')
                    self.assertEqual(set(inspect(engine).get_table_names()) - before,
                                     {'execution_policy_revisions', 'execution_policy_heads',
                                      'broker_capabilities', 'broker_reservations',
                                      'gateway_connection_revisions'})
                    with engine.connect() as db:
                        self.assertFalse(db.execute(text("SELECT broker_revoked FROM execution_assignments WHERE id='old-assignment'")).scalar_one())
                        self.assertIsNone(db.execute(text("SELECT execution_mode FROM workflow_runs WHERE id='old-run'")).scalar())
                        self.assertEqual(db.execute(text("SELECT ciphertext FROM workflow_launch_snapshots WHERE id='old-snapshot'")).scalar(), 'opaque-launch')
                    from sqlalchemy.exc import IntegrityError
                    with self.assertRaises(IntegrityError), engine.begin() as db:
                        db.execute(text("UPDATE workflow_runs SET execution_mode='unknown' WHERE id='old-run'"))
                    with sessions.begin() as db:
                        scope = ProjectScope(DEFAULT_PROJECT_ID)
                        policy = read_execution_policy(db, scope)
                        self.assertEqual(policy.default_mode, 'direct')
                        self.assertEqual(db.execute(text('SELECT name FROM projects WHERE id=:id'), {'id': DEFAULT_PROJECT_ID}).scalar(), 'Legacy')
                    command.downgrade(config, 'e201f1000013')
                    command.upgrade(config, 'head')
                    from app.database import BrokerCapability
                    with sessions.begin() as db:
                        db.add(BrokerCapability(id="test-broker", project_id=DEFAULT_PROJECT_ID,
                                                assignment_id="old-assignment", audience="model",
                                                bearer_hash="a" * 64, expires_at=1, revoked=True,
                                                scope={}, limits={}))
                    with self.assertRaisesRegex(RuntimeError, 'protected broker ledger'):
                        command.downgrade(config, 'e201f1000015')
                    with engine.begin() as db:
                        db.execute(text("DELETE FROM broker_capabilities WHERE id='test-broker'"))
                    with sessions.begin() as db:
                        update_execution_policy(db, scope, policy.revision_id,
                                                {'default_mode':'protected','minimum_mode':'protected'})
                    with self.assertRaisesRegex(RuntimeError, 'verified policy rollback'):
                        command.downgrade(config, 'e201f1000013')
            finally:
                if engine is not None:
                    engine.dispose()
                if admin is not None:
                    with admin.begin() as db:
                        db.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
                    admin.dispose()
