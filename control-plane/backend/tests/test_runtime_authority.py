"""Remote effects require persistent target authority and live database claims."""

from dataclasses import replace
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.database import database
import test_api as fixtures
import test_docker_jobs as docker_tests
from test_runtime_targets import target


def compete(url, key, barrier, queue):
    from app.runtime_claims import claim_runtime

    engine, sessions = database(url)
    try:
        barrier.wait(timeout=20)
        claim = claim_runtime(sessions, key, 100.0)
        queue.put(None if claim is None else (claim.owner, claim.epoch))
    finally:
        engine.dispose()


class RuntimeClaimTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(
            importlib.util.find_spec("app.runtime_claims"),
            "Database runtime claims are not implemented",
        )
        from app.database import RuntimeLease

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.url = os.environ.get(
            "CONTROL_TEST_DATABASE_URL", "sqlite:///" + self.tmp.name + "/claims.db"
        )
        self.admin = None
        if self.url.startswith("postgresql"):
            self.admin = create_engine(self.url)
            self.schema = "runtime_claim_" + uuid.uuid4().hex
            with self.admin.begin() as db:
                db.execute(text('CREATE SCHEMA "' + self.schema + '"'))
            self.url = (
                make_url(self.url)
                .update_query_dict({"options": "-csearch_path=" + self.schema})
                .render_as_string(hide_password=False)
            )
        self.engine, self.sessions = database(self.url)
        RuntimeLease.__table__.create(self.engine)
        self.addCleanup(self.close)
        self.key = "worker:" + str(uuid.uuid4())

    def close(self):
        self.engine.dispose()
        if self.admin:
            with self.admin.begin() as db:
                db.execute(text('DROP SCHEMA "' + self.schema + '" CASCADE'))
            self.admin.dispose()

    def test_independent_connections_compete_and_expired_owner_cannot_renew_or_release_new_claim(
        self,
    ):
        from app.runtime_claims import (
            claim_runtime,
            require_runtime_claim,
            renew_runtime_claim,
            release_runtime_claim,
        )

        first = claim_runtime(self.sessions, self.key, 100.0)
        self.assertIsNotNone(first)
        self.assertIsNone(claim_runtime(self.sessions, self.key, 159.0))
        second = claim_runtime(self.sessions, self.key, 161.0)
        self.assertEqual(second.epoch, first.epoch + 1)
        self.assertNotEqual(first.owner, second.owner)
        self.assertFalse(renew_runtime_claim(self.sessions, first, 161.0))
        with self.assertRaises(ValueError):
            require_runtime_claim(self.sessions, first, 161.0)
        release_runtime_claim(self.sessions, first)
        require_runtime_claim(self.sessions, second, 162.0)
        self.assertTrue(renew_runtime_claim(self.sessions, second, 200.0))
        require_runtime_claim(self.sessions, second, 250.0)
        release_runtime_claim(self.sessions, second)
        third = claim_runtime(self.sessions, self.key, 251.0)
        self.assertEqual(third.epoch, second.epoch + 1)

    def test_invalid_claim_key_and_time_never_creates_authority(self):
        from app.runtime_claims import claim_runtime

        for key, now in (
            ("arbitrary", 100.0),
            (self.key, float("nan")),
            (self.key, float("inf")),
        ):
            with self.subTest(key=key, now=now), self.assertRaises(ValueError):
                claim_runtime(self.sessions, key, now)

    @unittest.skipUnless(
        os.environ.get("CONTROL_TEST_DATABASE_URL", "").startswith("postgresql"),
        "real cross-process PostgreSQL claim competition",
    )
    def test_two_processes_have_one_winner(self):
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(2)
        queue = ctx.Queue()
        processes = [
            ctx.Process(target=compete, args=(self.url, self.key, barrier, queue))
            for _ in range(2)
        ]
        for p in processes:
            p.start()
        for p in processes:
            p.join(timeout=30)
            if p.is_alive():
                p.terminate()
                p.join()
                self.fail("Claim process did not finish")
            self.assertEqual(p.exitcode, 0)
        values = [queue.get(timeout=5) for _ in processes]
        self.assertEqual(sum(v is not None for v in values), 1)
        self.assertEqual(next(v for v in values if v is not None)[1], 1)


class RuntimeAuthorityTests(unittest.TestCase):
    setUp = fixtures.ControlPlaneTests.setUp
    tearDown = fixtures.ControlPlaneTests.tearDown
    arrange = docker_tests.JobBrokerTests.arrange
    submit = docker_tests.JobBrokerTests.submit

    def kube(self, *, submit=True):
        self.arrange()
        from app.database import Assignment, Worker
        from app.runtime_targets import TargetRegistry
        from app.job_broker import JobBroker

        path = Path(self.temporary.name) / "targets.json"
        path.write_text(json.dumps({"schema_version": 1, "targets": [target()]}))
        self.settings = replace(self.settings, runtime_targets_file=path)
        registry = TargetRegistry(self.settings)
        self.snapshot = registry.snapshot("test-kubernetes")
        with self.sessions.begin() as db:
            worker = db.get(Worker, db.get(Assignment, self.assignment).worker_id)
            worker.target_id = self.snapshot.id
            worker.target_snapshot = self.snapshot.model_dump()
        self.broker = JobBroker(
            self.sessions, self.settings, targets=registry, clock=lambda: self.now
        )
        return self.submit() if submit else None

    def test_job_pins_assigned_worker_target_and_absolute_deadline_before_effects(self):
        from app.database import RuntimeJob
        from app.project_context import decrypt

        receipt = self.kube()
        with self.sessions() as db:
            row = db.get(RuntimeJob, receipt.job_id)
            launch = decrypt(row.launch_ciphertext, self.settings)["launch"]
            self.assertEqual(
                (row.target_id, row.target_revision),
                (self.snapshot.id, self.snapshot.revision),
            )
            self.assertEqual(
                launch["target_snapshot"]["configuration"]["job_namespace"],
                "flumic-actions",
            )
            self.assertEqual(launch["deadline"], row.deadline)
            self.assertGreater(row.deadline, self.now)
        self.assertEqual(self.jobs.launches, [])

    def test_worker_snapshot_cannot_name_another_valid_target(self):
        from app.database import Worker, Assignment
        from app.runtime_targets import TargetRegistry
        from fastapi import HTTPException

        self.kube(submit=False)
        with self.sessions.begin() as db:
            worker = db.get(Worker, db.get(Assignment, self.assignment).worker_id)
            worker.target_snapshot = (
                TargetRegistry(self.settings).snapshot("local-docker").model_dump()
            )
        with self.assertRaises(HTTPException):
            self.submit()
        self.assertEqual(self.jobs.launches, [])

    def test_changed_job_pin_is_not_used_for_reconciliation(self):
        receipt = self.kube()
        from app.database import RuntimeJob

        with self.sessions.begin() as db:
            db.get(RuntimeJob, receipt.job_id).target_revision = "f" * 64
        self.broker.reconcile_once(self.jobs)
        self.assertEqual(self.jobs.launches, [])

    def test_child_resources_require_the_pinned_job_owner_uid(self):
        from app.runtime_resources import RuntimeResources
        from app.runtime_claims import claim_runtime

        receipt = self.kube()
        resources = RuntimeResources(
            self.sessions,
            self.snapshot,
            project_id=self.scope.project_id,
            clock=lambda: self.now,
        )
        claim = claim_runtime(self.sessions, "job:" + receipt.job_id, self.now)
        values = dict(
            namespace="flumic-actions",
            name="payload-fixture",
            uid="uid-payload",
            spec_digest="c" * 64,
            project_id=self.scope.project_id,
            job_id=receipt.job_id,
            claim=claim,
        )
        with self.assertRaises(ValueError):
            resources.bind(
                "ConfigMap", receipt.job_id + ":payload", owner_uid="foreign", **values
            )
        resources.bind(
            "Job", receipt.job_id, **{**values, "name": "job-fixture", "uid": "uid-job"}
        )
        with self.assertRaises(ValueError):
            resources.bind(
                "ConfigMap", receipt.job_id + ":payload", owner_uid="foreign", **values
            )
        resource = resources.bind(
            "ConfigMap", receipt.job_id + ":payload", owner_uid="uid-job", **values
        )
        self.assertEqual(resource.owner_uid, "uid-job")

    def test_new_pinned_launch_requires_the_same_encrypted_deadline(self):
        self.arrange()
        receipt = self.submit()
        from app.database import RuntimeJob
        from app.project_context import decrypt, encrypt

        with self.sessions.begin() as db:
            row = db.get(RuntimeJob, receipt.job_id)
            doc = decrypt(row.launch_ciphertext, self.settings)
            doc["launch"]["deadline"] = None
            row.launch_ciphertext = encrypt(doc, self.settings)
        self.broker.reconcile_once(self.jobs)
        self.assertEqual(self.jobs.launches, [])

    def test_no_host_lock_directory_is_needed_and_two_broker_roots_do_not_duplicate_job(
        self,
    ):
        self.arrange()
        from app.job_broker import JobBroker

        root = Path(self.temporary.name) / "absent-broker-root"
        first = JobBroker(
            self.sessions, self.settings, root=root, clock=lambda: self.now
        )
        receipt = first.submit(self.scope, self.assignment, self.digest)
        self.assertFalse(root.exists())
        second = JobBroker(
            self.sessions, self.settings, root=root / "other", clock=lambda: self.now
        )
        first.reconcile_once(self.jobs)
        second.reconcile_once(self.jobs)
        self.assertEqual(len(self.jobs.launches), 1)
        self.assertEqual(self.jobs.launches[0].job_id, receipt.job_id)

    def test_stale_claim_cannot_commit_capture_or_overwrite_newer_owner(self):
        self.assertIsNotNone(
            importlib.util.find_spec("app.runtime_claims"),
            "Missing database effect fence",
        )
        from app.runtime_claims import claim_runtime, release_runtime_claim
        from app.database import RuntimeJob

        self.arrange()
        receipt = self.submit()
        self.broker.reconcile_once(self.jobs)
        self.jobs.jobs[receipt.job_id] = "exited"
        original = self.jobs.collect
        stolen = []

        def steal(ref):
            self.now += 61
            stolen.append(claim_runtime(self.sessions, "job:" + ref.job_id, self.now))
            return original(ref)

        self.jobs.collect = steal
        self.broker.reconcile_once(self.jobs)
        self.assertIsNotNone(stolen[0])
        with self.sessions() as db:
            row = db.get(RuntimeJob, receipt.job_id)
            self.assertEqual(row.state, "running")
            self.assertIsNone(row.result_ciphertext)
        release_runtime_claim(self.sessions, stolen[0])
        self.jobs.collect = original
        self.broker.reconcile_once(self.jobs)
        self.assertEqual(self.submit().state, "completed")

    def test_remote_uid_spec_owner_and_project_are_not_replaceable(self):
        self.assertIsNotNone(
            importlib.util.find_spec("app.runtime_resources"),
            "Missing remote UID authority",
        )
        from app.runtime_resources import RuntimeResources
        from app.runtime_claims import claim_runtime

        receipt = self.kube()
        resources = RuntimeResources(
            self.sessions,
            self.snapshot,
            project_id=self.scope.project_id,
            clock=lambda: self.now,
        )
        claim = claim_runtime(self.sessions, "job:" + receipt.job_id, self.now)
        data = dict(
            namespace="flumic-actions",
            name="flumic-job-fixture",
            uid="uid-job-1",
            spec_digest="c" * 64,
            project_id=self.scope.project_id,
            job_id=receipt.job_id,
            claim=claim,
        )
        first = resources.bind("Job", receipt.job_id, **data)
        self.assertEqual(first.uid, "uid-job-1")
        self.assertEqual(resources.bind("Job", receipt.job_id, **data).uid, "uid-job-1")
        for update in (
            {"uid": "foreign"},
            {"spec_digest": "d" * 64},
            {"owner_uid": "foreign"},
            {"namespace": "other"},
            {"project_id": "foreign"},
        ):
            with self.subTest(update=update), self.assertRaises(ValueError):
                resources.bind("Job", receipt.job_id, **{**data, **update})
        self.assertIsNone(
            RuntimeResources(
                self.sessions,
                self.snapshot,
                project_id="foreign",
                clock=lambda: self.now,
            ).get("Job", receipt.job_id)
        )
        resources.record_phase(
            "Job",
            receipt.job_id,
            phase="running",
            expected_phase="prepared",
            claim=claim,
        )
        with self.assertRaises(ValueError):
            resources.record_phase(
                "Job",
                receipt.job_id,
                phase="captured",
                expected_phase="prepared",
                claim=claim,
            )

    def test_worker_change_requires_expected_digest_and_preserves_uid(self):
        from app.database import Assignment
        from app.runtime_resources import RuntimeResources
        from app.runtime_claims import claim_runtime

        self.kube()
        with self.sessions() as db:
            worker_id = db.get(Assignment, self.assignment).worker_id
        resources = RuntimeResources(
            self.sessions, self.snapshot, worker_id=worker_id, clock=lambda: self.now
        )
        claim = claim_runtime(self.sessions, "worker:" + worker_id, self.now)
        resources.bind(
            "Deployment",
            worker_id,
            namespace="flumic-workers",
            name="worker-fixture",
            uid="uid-worker",
            spec_digest="a" * 64,
            worker_id=worker_id,
            claim=claim,
        )
        with self.assertRaises(ValueError):
            resources.prepare_change(
                "Deployment",
                worker_id,
                expected_spec="x" * 64,
                new_spec="b" * 64,
                claim=claim,
            )
        pending = resources.prepare_change(
            "Deployment",
            worker_id,
            expected_spec="a" * 64,
            new_spec="b" * 64,
            claim=claim,
        )
        self.assertEqual(
            (pending.uid, pending.spec_digest, pending.pending_spec_digest),
            ("uid-worker", "a" * 64, "b" * 64),
        )
        with self.assertRaises(ValueError):
            resources.commit_change(
                "Deployment", worker_id, new_spec="c" * 64, claim=claim
            )
        completed = resources.commit_change(
            "Deployment", worker_id, new_spec="b" * 64, claim=claim
        )
        self.assertEqual(
            (completed.uid, completed.spec_digest, completed.pending_spec_digest),
            ("uid-worker", "b" * 64, None),
        )

    def test_legacy_persisted_launch_remains_readable(self):
        self.arrange()
        receipt = self.submit()
        from app.database import RuntimeJob
        from app.project_context import encrypt, decrypt

        with self.sessions.begin() as db:
            row = db.get(RuntimeJob, receipt.job_id)
            doc = decrypt(row.launch_ciphertext, self.settings)
            for key in ("target_id", "target_revision", "target_snapshot", "deadline"):
                doc["launch"].pop(key, None)
            row.target_id = "local-docker"
            row.target_revision = ""
            row.target_snapshot = {}
            row.launch_ciphertext = encrypt(doc, self.settings)
        self.broker.reconcile_once(self.jobs)
        self.assertEqual(len(self.jobs.launches), 1)


class RuntimeMigrationTests(unittest.TestCase):
    def test_upgrade_backfills_legacy_worker_and_downgrade_refuses_runtime_authority(
        self,
    ):
        from alembic import command
        from alembic.config import Config
        from sqlalchemy import inspect
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            url = os.environ.get(
                "CONTROL_MIGRATION_TEST_URL", "sqlite:///" + directory + "/migration.db"
            )
            admin = None
            if url.startswith("postgresql"):
                admin = create_engine(url)
                schema = "runtime_migration_" + uuid.uuid4().hex
                with admin.begin() as db:
                    db.execute(text('CREATE SCHEMA "' + schema + '"'))
                url = (
                    make_url(url)
                    .update_query_dict({"options": "-csearch_path=" + schema})
                    .render_as_string(hide_password=False)
                )
            try:
                cfg = Config(str(fixtures.ROOT / "control-plane/backend/alembic.ini"))
                with patch.dict(os.environ, {"CONTROL_DATABASE_URL": url}):
                    command.upgrade(cfg, "e201f1000011")
                    engine = create_engine(url)
                    worker = str(uuid.uuid4())
                    with engine.begin() as db:
                        db.execute(
                            text(
                                "INSERT INTO worker_instances (id,name,profile,revision_id,credential_hash,desired_state,created_at,target_id) VALUES (:id,'legacy-runtime-fixture','simple',:revision,'fixture','created',0,'local-docker')"
                            ),
                            {"id": worker, "revision": str(uuid.uuid4())},
                        )
                    engine.dispose()
                    command.upgrade(cfg, "head")
                    engine = create_engine(url)
                    self.assertTrue(
                        {"runtime_leases", "runtime_resource_bindings"}.issubset(
                            inspect(engine).get_table_names()
                        )
                    )
                    with engine.connect() as db:
                        value = db.execute(
                            text(
                                "SELECT target_snapshot FROM worker_instances WHERE id=:id"
                            ),
                            {"id": worker},
                        ).scalar_one()
                        self.assertEqual(
                            json.loads(value) if isinstance(value, str) else value, {}
                        )
                    engine.dispose()
                    command.downgrade(cfg, "e201f1000011")
                    command.upgrade(cfg, "head")
                    engine = create_engine(url)
                    with engine.begin() as db:
                        db.execute(
                            text(
                                "UPDATE worker_instances SET target_id='test-kubernetes' WHERE id=:id"
                            ),
                            {"id": worker},
                        )
                    engine.dispose()
                    with self.assertRaises(RuntimeError):
                        command.downgrade(cfg, "e201f1000011")
                    engine = create_engine(url)
                    self.assertIn(
                        "runtime_resource_bindings", inspect(engine).get_table_names()
                    )
                    engine.dispose()
            finally:
                if admin:
                    with admin.begin() as db:
                        db.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
                    admin.dispose()
