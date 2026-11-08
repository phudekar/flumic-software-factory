"""Pinned execution policy across logical launches and credential delivery."""
import unittest
from unittest.mock import Mock, patch
from fastapi import HTTPException
from sqlalchemy import select
import test_api as fixtures
import test_workflow_state as workflow_fixtures
from app import database as models
from app.project_context import decrypt, encrypt
from app.projects import ProjectScope


class ExecutionAdmissionTests(unittest.TestCase):
    setUp = fixtures.ControlPlaneTests.setUp
    tearDown = fixtures.ControlPlaneTests.tearDown
    setup_graph = workflow_fixtures.WorkflowStateTests.setup_graph
    publish = workflow_fixtures.WorkflowStateTests.publish
    worker = workflow_fixtures.WorkflowStateTests.worker
    arrange = workflow_fixtures.WorkflowStateTests.arrange
    start = workflow_fixtures.WorkflowStateTests.start

    def change_policy(self, scope, default='protected', minimum='direct'):
        path = f'/api/v1/projects/{scope.project_id}/execution-policy'
        old = self.client.get(path).json()
        response = self.client.put(path, json={'expected_revision':old['revision_id'],
                                  'default_mode':default,'minimum_mode':minimum})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def snapshot(self, scope, run):
        from app.workflow_definitions import read_definition_pin
        with self.sessions() as db:
            return read_definition_pin(db, scope, run.snapshot_id, self.settings)

    def test_manual_launch_pins_mode_and_policy_revision(self):
        scope, _, definition = self.arrange()
        path = f'/api/v1/projects/{scope.project_id}/workflow-runs'
        response = self.client.post(path, json={'revision_id':definition['revision_id'],
                                               'execution_mode':'direct'})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()['execution_mode'], 'direct')
        with self.sessions() as db:
            run = db.get(models.WorkflowRun, response.json()['id'])
        frozen = self.snapshot(scope, run)['execution_pin']
        self.assertEqual(frozen['mode'], 'direct')
        self.assertEqual(frozen['policy_revision_id'], 'direct-v1')
        self.change_policy(scope)
        public = self.client.get(path + '/' + run.id).json()
        self.assertEqual(public['execution_policy_revision_id'], frozen['policy_revision_id'])
        self.assertEqual(public['execution_mode'], 'direct')
        self.client.headers['Idempotency-Key'] = 'explicit-direct-under-protected-default'
        response = self.client.post(path, json={'revision_id':definition['revision_id'],
                                               'execution_mode':'direct'})
        self.assertEqual(response.status_code, 202, response.text)
        self.client.headers['Idempotency-Key'] = 'unqualified-protected'
        blocked = self.client.post(path, json={'revision_id':definition['revision_id'],
                                              'execution_mode':'protected'})
        self.assertEqual(blocked.status_code, 409)

    def test_trigger_and_followup_style_pinned_launches_apply_current_minimum(self):
        from factory.domain.execution import ExecutionPin
        from app.workflow_definitions import pin_definition
        scope, _, definition = self.arrange()
        with self.sessions.begin() as db:
            template = pin_definition(db, scope, definition['revision_id'],
                                      fixtures.Configurations(self.settings.root), self.settings)
            template_id = template.id
        self.change_policy(scope, minimum='protected')
        for index, parent in enumerate((None, ExecutionPin(mode='direct', policy_revision_id='direct-v1'))):
            with self.subTest(parent=parent):
                with self.assertRaises(HTTPException) as error:
                    self.store.start_pinned_run(scope, template_id, {}, key='unqualified-' + str(index), parent_pin=parent)
                self.assertEqual(error.exception.status_code, 409)
        with self.sessions() as db:
            self.assertEqual(list(db.scalars(select(models.WorkflowRun))), [])

    def test_logical_replay_keeps_original_mode_and_clones_snapshot(self):
        scope, _, definition = self.arrange()
        original = self.start(scope, definition)
        original_pin = self.snapshot(scope, original)
        self.change_policy(scope)
        replay = self.store.start_pinned_run(scope, original.snapshot_id, {}, key='logical-replay')
        self.assertNotEqual(replay.snapshot_id, original.snapshot_id)
        self.assertEqual(self.snapshot(scope, replay)['execution_pin']['mode'], 'direct')
        self.assertEqual(self.snapshot(scope, original), original_pin)
        with self.assertRaises(HTTPException):
            self.store.start_pinned_run(scope, original.snapshot_id, {}, key='different-mode', requested_mode='protected')
        self.change_policy(scope, minimum='protected')
        with self.assertRaises(HTTPException) as error:
            self.store.start_pinned_run(scope, original.snapshot_id, {}, key='blocked-replay')
        self.assertEqual(error.exception.status_code, 409)
        # Old idempotency acknowledgments retain their accepted pin, not current policy.
        self.assertEqual(self.start(scope, definition).id, original.id)

    def test_queued_direct_run_keeps_pin_after_policy_change_and_old_wire_shape(self):
        from app.project_context import resolve_pinned_context
        scope, _, definition = self.arrange()
        run = self.start(scope, definition)
        before = self.snapshot(scope, run)['execution_pin']
        self.change_policy(scope, minimum='protected')
        advance = self.store.advance_run(scope, run.id)
        self.assertEqual(len(advance.ready), 1)
        with self.sessions() as db:
            attempt = db.get(models.StageAttempt, advance.ready[0].execution_id)
            assignment = db.get(models.Assignment, attempt.assignment_id)
            context = resolve_pinned_context(db, scope, assignment.context_id, self.settings,
                                             assignment_id=assignment.id)
            wire = context.model_dump(mode='json')
            self.assertNotIn('execution_mode', wire)
            self.assertNotIn('execution_policy_revision_id', wire)
        self.assertEqual(self.snapshot(scope, run)['execution_pin'], before)

    def test_protected_or_forged_direct_context_never_reads_or_wraps_provider_values(self):
        from app.project_context import resolve_pinned_context
        scope, _, definition = self.arrange()
        run = self.start(scope, definition)
        advance = self.store.advance_run(scope, run.id)
        for context_mode in ('protected', 'direct'):
            with self.sessions.begin() as db:
                snapshot = db.get(models.WorkflowLaunchSnapshot, run.snapshot_id)
                launch = decrypt(snapshot.ciphertext, self.settings)
                launch['execution_pin'] = {'mode':'protected','policy_revision_id':'test-policy'}
                snapshot.ciphertext = encrypt(launch, self.settings)
                attempt = db.get(models.StageAttempt, advance.ready[0].execution_id)
                assignment = db.get(models.Assignment, attempt.assignment_id)
                pin = db.get(models.ActionContextSnapshot, assignment.context_id)
                document = decrypt(pin.ciphertext, self.settings)
                document['context']['execution_mode'] = context_mode
                document['context']['execution_policy_revision_id'] = 'test-policy'
                document['grants'] = {'openai':'must-not-be-resolved'}
                pin.ciphertext = encrypt(document, self.settings)
                identity, assignment_id = pin.id, assignment.id
            for deliver in (None, Mock()):
                with self.sessions() as db, patch('app.project_context.require_secret') as secret, patch('app.project_context.read_credential') as read:
                    with self.assertRaises(HTTPException) as error:
                        resolve_pinned_context(db, scope, identity, self.settings, assignment_id=assignment_id, deliver=deliver)
                    self.assertEqual(error.exception.status_code, 409)
                    secret.assert_not_called()
                    read.assert_not_called()
                    if deliver is not None:
                        deliver.assert_not_called()

    def test_legacy_logical_replay_keeps_direct_mode_without_rewriting_original(self):
        scope, _, definition = self.arrange()
        run = self.start(scope, definition)
        with self.sessions.begin() as db:
            pin = db.get(models.WorkflowLaunchSnapshot, run.snapshot_id)
            legacy = decrypt(pin.ciphertext, self.settings)
            legacy.pop('execution_pin')
            pin.ciphertext = encrypt(legacy, self.settings)
        self.change_policy(scope)
        replay = self.store.start_pinned_run(scope, run.snapshot_id, {}, key='legacy-replay')
        self.assertEqual(self.snapshot(scope, replay)['execution_pin']['mode'], 'direct')
        self.assertEqual(self.snapshot(scope, run), legacy)

    def test_direct_run_count_uses_pinned_mode_projection(self):
        scope, _, definition = self.arrange()
        run = self.start(scope, definition)
        with self.sessions.begin() as db:
            row = db.get(models.WorkflowRun, run.id)
            self.assertEqual(row.execution_mode, 'direct')
            # A future qualified protected run must not inflate the direct warning.
            row.execution_mode = 'protected'
            pin = db.get(models.WorkflowLaunchSnapshot, run.snapshot_id)
            value = decrypt(pin.ciphertext, self.settings)
            value['execution_pin']['mode'] = 'protected'
            pin.ciphertext = encrypt(value, self.settings)
        response = self.client.get(f'/api/v1/projects/{scope.project_id}/execution-policy')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['admitted_direct_runs'], 0)

    def test_unqualified_protected_run_never_creates_worker_assignment(self):
        scope, _, definition = self.arrange()
        run = self.start(scope, definition)
        with self.sessions.begin() as db:
            row = db.get(models.WorkflowRun, run.id)
            row.execution_mode = 'protected'
            snapshot = db.get(models.WorkflowLaunchSnapshot, run.snapshot_id)
            value = decrypt(snapshot.ciphertext, self.settings)
            value['execution_pin']['mode'] = 'protected'
            snapshot.ciphertext = encrypt(value, self.settings)
        result = self.store.advance_run(scope, run.id)
        self.assertEqual(result.ready, [])
        self.assertEqual(result.state, 'failed')
        with self.sessions() as db:
            self.assertEqual(list(db.scalars(select(models.Assignment))), [])

    def test_invalid_pins_and_protected_parent_minimum_fail_closed(self):
        from app.execution_admission import admit_execution, read_execution_pin
        from factory.domain.execution import ExecutionPin
        scope, _, _ = self.arrange()
        self.assertEqual(read_execution_pin({}).mode, 'direct')
        for invalid_launch in (None, []):
            with self.assertRaises(HTTPException):
                read_execution_pin(invalid_launch)
        for value in (None, {'mode':'unknown','policy_revision_id':'policy'}):
            with self.assertRaises(HTTPException):
                read_execution_pin({'execution_pin':value})
        with self.sessions() as db:
            parent = ExecutionPin(mode='protected', policy_revision_id='parent-policy')
            for requested in (None, 'direct'):
                with self.assertRaises(HTTPException):
                    admit_execution(db, scope, {}, requested, parent)

    def test_assignment_identity_is_checked_before_secret_delivery(self):
        from app.project_context import resolve_pinned_context
        scope, _, definition = self.arrange()
        run = self.start(scope, definition)
        ready = self.store.advance_run(scope, run.id).ready[0]
        with self.sessions.begin() as db:
            attempt = db.get(models.StageAttempt, ready.execution_id)
            assignment = db.get(models.Assignment, attempt.assignment_id)
            context = db.get(models.ActionContextSnapshot, assignment.context_id)
            document = decrypt(context.ciphertext, self.settings)
            document['grants'] = {'openai':'must-not-be-resolved'}
            context.ciphertext = encrypt(document, self.settings)
            identity, assignment_id = context.id, assignment.id
        with self.sessions() as db, patch('app.project_context.require_secret') as secret:
            deliver = Mock()
            with self.assertRaises(HTTPException):
                resolve_pinned_context(db, scope, identity, self.settings, assignment_id=assignment_id,
                                       deliver=deliver, expected_authority={'run_id':'foreign-run'})
            secret.assert_not_called()
            deliver.assert_not_called()

    def test_qualification_rejects_missing_stale_or_tampered_evidence(self):
        from factory.domain.execution import ExecutionPin, TargetQualification
        from app.execution_admission import qualification_digest, require_target_qualification, PROTECTED_CAPABILITIES
        target = TargetQualification(id='qualification', target_id='owned-target', runtime_digest='sha256:original',
                                     capabilities=frozenset(PROTECTED_CAPABILITIES), expires_at=1001.0)
        pin = ExecutionPin(mode='protected', policy_revision_id='policy', target_qualification_id=target.id,
                           target_qualification_digest=qualification_digest(target))
        require_target_qualification(pin, target, 1000.0)
        variants = [target.model_copy(update={'id':'other'}), target.model_copy(update={'expires_at':999.0}),
                    target.model_copy(update={'runtime_digest':'sha256:changed'}),
                    target.model_copy(update={'capabilities':frozenset()})]
        for changed in variants:
            with self.subTest(changed=changed), self.assertRaises(HTTPException):
                require_target_qualification(pin, changed, 1000.0)
        with self.assertRaises(HTTPException):
            require_target_qualification(pin.model_copy(update={'target_qualification_id':None}), target, 1000.0)
        insufficient = target.model_copy(update={'capabilities':frozenset()})
        with self.assertRaises(HTTPException):
            require_target_qualification(pin.model_copy(update={'target_qualification_digest':qualification_digest(insufficient)}), insufficient, 1000.0)
        for now in (1001.0, float('nan')):
            with self.assertRaises(HTTPException):
                require_target_qualification(pin, target, now)
