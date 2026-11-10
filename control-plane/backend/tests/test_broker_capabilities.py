"""Broker authorization, dispatch and accounting against the control database."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import os
from threading import Barrier, Event
import time
import unittest
from unittest.mock import patch

from sqlalchemy import select

import test_api as fixtures
import test_workflow_state as workflow_fixtures
from app import broker_capabilities as broker, database as models
from app.project_context import decrypt, encrypt
from factory.domain.broker import CapabilityScope, SessionLimits


class BrokerCapabilitiesTests(unittest.TestCase):
    setUp = fixtures.ControlPlaneTests.setUp
    tearDown = fixtures.ControlPlaneTests.tearDown
    setup_graph = workflow_fixtures.WorkflowStateTests.setup_graph
    publish = workflow_fixtures.WorkflowStateTests.publish
    worker = workflow_fixtures.WorkflowStateTests.worker
    arrange = workflow_fixtures.WorkflowStateTests.arrange
    start = workflow_fixtures.WorkflowStateTests.start

    def assignment(self):
        project, _, definition = self.arrange()
        run = self.start(project, definition)
        attempt_id = self.store.advance_run(project, run.id).ready[0].execution_id
        with self.sessions.begin() as db:
            # Protected admission remains gated until runtime qualification.
            stored_run = db.get(models.WorkflowRun, run.id)
            stored_run.execution_mode = "protected"
            snapshot = db.get(models.WorkflowLaunchSnapshot, stored_run.snapshot_id)
            launch = decrypt(snapshot.ciphertext, self.settings)
            launch["execution_pin"]["mode"] = "protected"
            snapshot.ciphertext = encrypt(launch, self.settings)
            attempt = db.get(models.StageAttempt, attempt_id)
            attempt.state = "running"
            assignment = db.get(models.Assignment, attempt.assignment_id)
            assignment.status = "running"
            assignment.worker_id = db.scalar(select(models.Worker.id))
            assignment.started_at = time.time()
            assignment.expires_at = time.time() + 30
            scope = CapabilityScope(project_id=project.project_id, run_id=run.id,
                stage_id=attempt.stage_id, attempt_id=attempt.id,
                assignment_id=assignment.id, audience="model", operations=("model.infer",))
        self.limits = SessionLimits(request_count=3, concurrent_requests=2,
            input_tokens=10, output_tokens=10, ttl_seconds=300)
        return project, scope

    def issue(self, scope, limits=None):
        with self.sessions.begin() as db:
            return broker.issue_capability(db, scope, limits or self.limits, settings=self.settings)

    def reserve(self, token, request_id, audience="model", digest="a" * 64, operation="model.infer"):
        with self.sessions.begin() as db:
            return broker.reserve_request(db, token, audience, request_id, 6, 6,
                operation=operation, request_digest=digest, settings=self.settings)

    def dispatch(self, token, reservation):
        with self.sessions.begin() as db:
            return broker.mark_dispatched(db, token, "model", reservation.id,
                settings=self.settings)

    def settle(self, reservation, outcome, usage=None):
        with self.sessions.begin() as db:
            broker.settle_request(db, reservation.id, usage, outcome)

    def test_scope_expiry_and_hash_only_storage(self):
        _, scope = self.assignment()
        for field in ("project_id", "run_id", "stage_id", "attempt_id", "assignment_id"):
            with self.subTest(field=field), self.assertRaises(broker.CapabilityDenied):
                self.issue(scope.model_copy(update={field: "wrong"}))
        bearer = self.issue(scope)
        with self.sessions() as db:
            row = db.scalar(select(models.BrokerCapability))
            self.assertEqual(row.bearer_hash, sha256(bearer.encode()).hexdigest())
            self.assertLessEqual(row.expires_at, db.get(models.Assignment, scope.assignment_id).expires_at)
        with self.assertRaises(broker.CapabilityDenied):
            self.reserve(bearer, "one", audience="repository")
        with self.sessions.begin() as db:
            db.get(models.Assignment, scope.assignment_id).expires_at = time.time() - 1
        with self.assertRaises(broker.CapabilityDenied):
            self.reserve(bearer, "one")

    def test_stage_deadline_fences_a_still_live_lease(self):
        _, scope = self.assignment()
        bearer = self.issue(scope)
        with self.sessions.begin() as db:
            assignment = db.get(models.Assignment, scope.assignment_id)
            assignment.started_at = time.time() - 86400
            assignment.expires_at = time.time() + 60
        with self.assertRaises(broker.CapabilityDenied):
            self.reserve(bearer, "one")

    def test_renewal_preserves_reservations_and_dispatch_identity(self):
        _, scope = self.assignment()
        first = self.issue(scope)
        reservation = self.reserve(first, "one")
        self.assertTrue(self.dispatch(first, reservation))
        second = self.issue(scope)
        with self.assertRaises(broker.CapabilityDenied):
            self.reserve(first, "one")
        with self.assertRaises(broker.CapabilityDenied):
            self.reserve(second, "one", digest="b" * 64)
        duplicate = self.reserve(second, "one")
        self.assertEqual(duplicate.id, reservation.id)
        self.assertFalse(self.dispatch(second, duplicate))
        with self.assertRaises(broker.CapabilityDenied):
            self.reserve(second, "two")

    def test_cancel_revokes_and_allows_independent_reconciliation(self):
        project, scope = self.assignment()
        bearer = self.issue(scope)
        reservation = self.reserve(bearer, "one")
        self.assertTrue(self.dispatch(bearer, reservation))
        self.store.cancel_run(project, scope.run_id)
        with self.sessions() as db:
            self.assertTrue(db.scalar(select(models.BrokerCapability)).revoked)
        with self.assertRaises(broker.CapabilityDenied):
            self.reserve(bearer, "two")
        with self.assertRaises(broker.CapabilityDenied):
            self.issue(scope)
        self.settle(reservation, "completed", {"input_tokens": 2, "output_tokens": 3})

    def test_assignment_revocation_prevents_first_issue(self):
        _, scope = self.assignment()
        with self.sessions.begin() as db:
            broker.revoke_assignment(db, scope.assignment_id)
        with self.assertRaises(broker.CapabilityDenied):
            self.issue(scope)

    def test_mcp_capability_is_bound_to_server_and_operation(self):
        _, scope = self.assignment()
        delegated = scope.model_copy(update={"audience": "mcp:review"})
        with self.assertRaises(broker.CapabilityDenied):
            self.issue(delegated)
        scope = delegated.model_copy(update={"operations": ("repository.read",)})
        bearer = self.issue(scope)
        for audience, operation in (("repository", "repository.read"), ("mcp:other", "repository.read"),
                                    ("mcp:review", "repository.write")):
            with self.subTest(audience=audience, operation=operation):
                with self.assertRaises(broker.CapabilityDenied):
                    self.reserve(bearer, "one", audience=audience, operation=operation)
        self.assertEqual(self.reserve(bearer, "one", audience=scope.audience, operation="repository.read").scope, scope)

    def test_stopped_worker_loses_broker_authority(self):
        _, scope = self.assignment()
        bearer = self.issue(scope)
        reservation = self.reserve(bearer, "one")
        with self.sessions.begin() as db:
            db.scalar(select(models.Worker)).desired_state = "stopped"
        with self.assertRaises(broker.CapabilityDenied):
            self.issue(scope)
        with self.assertRaises(broker.CapabilityDenied):
            self.dispatch(bearer, reservation)

    def test_direct_run_cannot_issue_capability(self):
        _, scope = self.assignment()
        with self.sessions.begin() as db:
            db.get(models.WorkflowRun, scope.run_id).execution_mode = "direct"
        with self.assertRaises(broker.CapabilityDenied):
            self.issue(scope)

    def test_uncertainty_retains_tokens_until_reconciled(self):
        _, scope = self.assignment()
        bearer = self.issue(scope)
        reservation = self.reserve(bearer, "one")
        self.assertTrue(self.dispatch(bearer, reservation))
        self.settle(reservation, "uncertain")
        with self.assertRaises(broker.CapabilityDenied):
            self.reserve(bearer, "two")
        self.settle(reservation, "completed", {"input_tokens": 2, "output_tokens": 3})
        self.settle(reservation, "completed", {"input_tokens": 2, "output_tokens": 3})
        self.assertIsNotNone(self.reserve(bearer, "two"))

    @unittest.skipUnless(os.environ.get("CONTROL_TEST_DATABASE_URL", "").startswith("postgresql"),
                         "requires independent PostgreSQL connections")
    def test_concurrent_reservations_cannot_overspend(self):
        _, scope = self.assignment()
        bearer = self.issue(scope)
        barrier = Barrier(2, timeout=5)

        def reserve(request_id):
            barrier.wait()
            try:
                return self.reserve(bearer, request_id)
            except broker.CapabilityDenied:
                return None

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(reserve, ("one", "two")))
        self.assertEqual(sum(result is not None for result in results), 1)

    @unittest.skipUnless(os.environ.get("CONTROL_TEST_DATABASE_URL", "").startswith("postgresql"),
                         "requires independent PostgreSQL connections")
    def test_concurrent_dispatch_has_one_winner(self):
        _, scope = self.assignment()
        bearer = self.issue(scope)
        reservation = self.reserve(bearer, "one")
        barrier = Barrier(2, timeout=5)

        def dispatch(_):
            barrier.wait()
            return self.dispatch(bearer, reservation)

        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(sum(pool.map(dispatch, range(2))), 1)

    @unittest.skipUnless(os.environ.get("CONTROL_TEST_DATABASE_URL", "").startswith("postgresql"),
                         "requires independent PostgreSQL connections")
    def test_dispatch_rechecks_revocation_after_waiting_for_cancellation(self):
        from app import workflow_state

        project, scope = self.assignment()
        bearer = self.issue(scope)
        reservation = self.reserve(bearer, "one")
        cancelling = Event()
        dispatching = Event()
        revoke = workflow_state.revoke_run

        def hold_cancellation(db, project_id, run_id):
            cancelling.set()
            if not dispatching.wait(timeout=5):
                raise TimeoutError("Dispatch never reached its transaction fence")
            revoke(db, project_id, run_id)

        def signal_dispatch(db):
            dispatching.set()
            workflow_state.transition_lock(db)

        with (
            patch.object(workflow_state, "revoke_run", hold_cancellation),
            patch.object(broker, "_lock", signal_dispatch),
            ThreadPoolExecutor(2) as pool,
        ):
            cancel = pool.submit(self.store.cancel_run, project, scope.run_id)
            self.assertTrue(cancelling.wait(timeout=5))
            dispatch = pool.submit(self.dispatch, bearer, reservation)
            self.assertEqual(cancel.result(timeout=10).state, "cancelling")
            with self.assertRaises(broker.CapabilityDenied):
                dispatch.result(timeout=10)
