"""Durable one-time job contracts; Docker/provider integrations are fakes."""

import json
import tempfile
import unittest
from pathlib import Path
from fastapi import HTTPException
from unittest.mock import Mock
import test_api as fixtures

IMAGE = "example/check@sha256:" + "a" * 64


class DockerJobRuntimeTests(unittest.TestCase):
    def setUp(self):
        from app.adapters.docker_jobs import DockerJobRuntime
        from app.job_contracts import JobLaunch

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = Mock()
        self.container = Mock(id="container-id", status="created")
        self.client.containers.get.side_effect = self.not_found
        self.client.containers.create.return_value = self.container
        self.runtime = DockerJobRuntime(
            Path(self.tmp.name), Path(self.tmp.name), "deployment", client=self.client
        )
        self.launch = JobLaunch(
            job_id="a" * 64,
            project_id="p",
            execution_id="e",
            spec_digest="b" * 64,
            image=IMAGE,
            command=["python", "/app/run.py"],
            inputs={"test": True},
        )

    @staticmethod
    def not_found(*args, **kwargs):
        from docker.errors import NotFound

        raise NotFound("missing")

    def attributes(self, status="created", **state):
        return {
            "Config": {"Image": IMAGE, "Labels": self.runtime.labels(self.launch)},
            "State": {"Status": status, "ExitCode": 0, "OOMKilled": False, **state},
        }

    def test_reserved_job_bridge_names_are_rejected(self):
        from app.adapters.docker_jobs import DockerJobRuntime

        for network in ("host", "bridge", "none"):
            with self.subTest(network=network), self.assertRaises(ValueError):
                DockerJobRuntime(
                    Path(self.tmp.name),
                    Path(self.tmp.name),
                    "deployment",
                    client=self.client,
                    bridge=network,
                )

    def test_create_policy_and_same_identity_reattach(self):
        ref = self.runtime.ensure(self.launch)
        options = self.client.containers.create.call_args.kwargs
        self.assertEqual(options["user"], "10001:10001")
        self.assertTrue(options["read_only"])
        self.assertEqual(options["network_mode"], "none")
        self.assertEqual(options["cap_drop"], ["ALL"])
        self.assertEqual(options["environment"], {})
        self.assertFalse(options["auto_remove"])
        self.assertNotIn("/var/run/docker.sock", str(options))
        self.assertEqual(len(options["volumes"]), 3)
        self.assertEqual(options["mem_limit"], 512 * 1024 * 1024)
        self.container.attrs = self.attributes()
        self.client.containers.get.side_effect = None
        self.client.containers.get.return_value = self.container
        self.assertEqual(self.runtime.ensure(self.launch), ref)
        self.assertEqual(self.client.containers.create.call_count, 1)

    def test_reattach_rejects_foreign_labels_or_image(self):
        self.client.containers.get.side_effect = None
        self.client.containers.get.return_value = self.container
        for field in ("deployment", "execution", "spec"):
            self.container.attrs = self.attributes()
            self.container.attrs["Config"]["Labels"]["flumic.job." + field] = "wrong"
            with self.assertRaises(ValueError):
                self.runtime.ensure(self.launch)
        self.container.attrs = self.attributes()
        self.container.attrs["Config"]["Image"] = "other@sha256:" + "c" * 64
        with self.assertRaises(ValueError):
            self.runtime.ensure(self.launch)
        self.container.start.assert_not_called()

    def capture(self, content):
        ref = self.runtime.ensure(self.launch)
        directory = Path(self.tmp.name) / self.launch.job_id / "output"
        (directory / "result.json").write_bytes(content)
        self.container.attrs = self.attributes("exited")
        self.client.containers.get.side_effect = None
        self.client.containers.get.return_value = self.container
        self.container.logs.side_effect = lambda **kwargs: iter(
            [b"hello", b"x" * 300000]
        )
        return self.runtime.collect(ref)

    def test_bounded_result_and_separate_truncated_logs(self):
        result = self.capture(b'{"output":{"ok":true},"usage":[]}')
        self.assertEqual(result.output, {"ok": True})
        self.assertLessEqual(len(result.stdout.encode()), 262144)
        self.assertTrue(result.stdout_truncated)
        self.assertTrue(result.stderr_truncated)

    def test_invalid_result_symlink_size_and_schema_fail_explicitly(self):
        for payload in (
            b"{}",
            b'{"output":[]}',
            b'{"output":{"x":NaN}}',
            b"x" * 1048577,
        ):
            with self.subTest(payload=payload[:40]):
                self.assertIsNotNone(self.capture(payload).error)
        path = Path(self.tmp.name) / self.launch.job_id / "output/result.json"
        path.unlink()
        path.symlink_to(Path(self.tmp.name) / "elsewhere")
        self.assertIsNotNone(
            self.runtime.collect(self.runtime.reference(self.launch)).error
        )

    def test_all_capture_protocol_keys_survive_secret_collisions(self):
        from app.job_contracts import JobCapture, sanitize_capture

        for secret in ("error", "output", "usage", "stdout", "exit_code"):
            with self.subTest(secret=secret):
                result, failed = sanitize_capture(
                    JobCapture(output={"value": secret}), [secret]
                )
                self.assertFalse(failed)
                self.assertEqual(result["output"], {"value": "[REDACTED]"})
                self.assertEqual(
                    set(result),
                    {
                        "error",
                        "exit_code",
                        "oom",
                        "output",
                        "usage",
                        "stdout",
                        "stderr",
                        "stdout_truncated",
                        "stderr_truncated",
                    },
                )

    def test_parser_recursion_is_a_failed_capture_with_logs(self):
        from unittest.mock import patch

        with patch(
            "app.adapters.docker_jobs.json.loads",
            side_effect=RecursionError("deep JSON"),
        ):
            capture = self.capture(b'{"output":{}}')
        self.assertIsNotNone(capture.error)
        self.assertTrue(capture.stdout_truncated)

    def test_deep_json_is_a_failed_capture_on_deployment_python(self):
        capture = self.capture(
            b'{"output":{"nested":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}}"
        )
        self.assertIsNotNone(capture.error)
        self.assertTrue(capture.stdout_truncated)

    def test_nonzero_and_oom_are_not_success(self):
        self.capture(b'{"output":{"ok":true}}')
        for values in ({"ExitCode": 1}, {"OOMKilled": True}):
            self.container.attrs = self.attributes("exited", **values)
            capture = self.runtime.collect(self.runtime.reference(self.launch))
            self.assertIsNotNone(capture.error)
            self.assertTrue(capture.stdout_truncated)

    def test_create_conflict_recovers_the_verified_created_container(self):
        from docker.errors import APIError, NotFound

        self.container.attrs = self.attributes("created")
        self.client.containers.get.side_effect = [NotFound("missing"), self.container]
        self.client.containers.create.side_effect = APIError(
            "conflict", response=Mock(status_code=409)
        )
        self.assertEqual(
            self.runtime.ensure(self.launch), self.runtime.reference(self.launch)
        )
        self.container.start.assert_called_once()

    def test_stop_escalates_and_created_jobs_are_removed_without_start(self):
        self.client.containers.get.side_effect = None
        self.client.containers.get.return_value = self.container
        self.container.attrs = self.attributes("running")
        self.runtime.terminate(self.runtime.reference(self.launch), grace_seconds=5)
        self.container.stop.assert_called_once_with(timeout=5)
        self.container.kill.assert_called_once()
        self.container.attrs = self.attributes("created")
        self.runtime.terminate(self.runtime.reference(self.launch))
        self.container.remove.assert_called_once()
        self.container.start.assert_not_called()

    def test_cleanup_removes_only_terminal_job_files(self):
        self.capture(b'{"output":{"ok":true}}')
        self.runtime.cleanup(self.runtime.reference(self.launch))
        self.assertFalse((Path(self.tmp.name) / self.launch.job_id).exists())


class FakeJobRuntime:
    def __init__(self):
        self.jobs = {}
        self.launches = []
        self.cleaned = []
        self.terminated = []
        self.crash = None

    def ensure(self, launch):
        from app.job_contracts import RuntimeJobRef

        ref = RuntimeJobRef(
            **{k: getattr(launch, k) for k in RuntimeJobRef.model_fields}
        )
        if launch.job_id not in self.jobs:
            self.jobs[launch.job_id] = "running"
            self.launches.append(launch)
        if self.crash == "create":
            self.crash = None
            raise RuntimeError("Broker transport lost after create/start")
        return ref

    def inspect(self, ref):
        from app.job_contracts import RuntimeJobState

        return RuntimeJobState(state=self.jobs.get(ref.job_id, "missing"), exit_code=0)

    def collect(self, ref):
        from app.job_contracts import JobCapture

        if self.crash == "collect":
            self.crash = None
            raise RuntimeError("Broker lost after capture")
        return JobCapture(
            output={"done": True, "token": "private-job-key"}, stdout="private-job-key"
        )

    def terminate(self, ref, grace_seconds=5):
        self.terminated.append(ref.job_id)
        if ref.job_id in self.jobs:
            self.jobs[ref.job_id] = "exited"

    def cleanup(self, ref):
        self.cleaned.append(ref.job_id)
        self.jobs.pop(ref.job_id, None)


class JobBrokerTests(unittest.TestCase):
    setUp = fixtures.ControlPlaneTests.setUp
    tearDown = fixtures.ControlPlaneTests.tearDown

    def arrange(self, image=IMAGE, command=None):
        import test_workflow_definitions as definitions
        import test_worker_pools as pools
        from app.workflow_state import WorkflowState
        from app.projects import ProjectScope
        from app import database as models
        from app.job_broker import JobBroker

        project, base, repos, pool, graph = (
            definitions.WorkflowDefinitionTests.setup_graph(self)
        )
        stage = graph["stages"][1]
        stage.update(
            id="job",
            after=[],
            inputs={"hello": "world"},
            retries=1,
            image=image,
            command=command or ["python", "/app/run.py"],
        )
        graph["stages"] = [stage]
        secret = self.client.post(
            base + "/secrets",
            json={"name": "job-model", "purpose": "openai", "value": "private-job-key"},
        ).json()["id"]
        stage["secrets"] = {"openai": secret}
        published = definitions.WorkflowDefinitionTests.publish(self, base, graph)
        identity, _ = pools.WorkerPoolTests.worker(self, pool)
        with self.sessions.begin() as db:
            worker = db.get(models.Worker, identity)
            worker.capabilities = {
                **worker.capabilities,
                "executors": ["builtin@1", "docker@1"],
                "actions": ["core.echo@1", "docker.run@1"],
            }
        self.scope = ProjectScope(project)
        self.now = 1000.0
        self.store = WorkflowState(
            self.sessions,
            self.settings,
            fixtures.Configurations(self.settings.root),
            clock=lambda: self.now,
        )
        self.store.enable_dynamic_repository(self.scope, repos["frontend"]["id"])
        self.run = self.store.start_run(
            self.scope, published["revision_id"], {}, key="job-run"
        )
        invocation = self.store.advance_run(self.scope, self.run.id).ready[0]
        self.execution = invocation.execution_id
        with self.sessions.begin() as db:
            attempt = db.get(models.StageAttempt, self.execution)
            assignment = db.get(models.Assignment, attempt.assignment_id)
            assignment.status, assignment.worker_id, assignment.expires_at = (
                "running",
                identity,
                self.now + 60,
            )
            self.assignment, self.digest = assignment.id, attempt.spec_digest
        self.jobs = FakeJobRuntime()
        self.broker = JobBroker(self.sessions, self.settings, clock=lambda: self.now)

    def submit(self, digest=None):
        return self.broker.submit(self.scope, self.assignment, digest or self.digest)

    def test_submit_rejects_foreign_and_expired_assignments(self):
        self.arrange()
        from app.projects import ProjectScope
        from app.database import Assignment

        with self.assertRaises(HTTPException):
            self.broker.submit(ProjectScope("foreign"), self.assignment, self.digest)
        with self.sessions.begin() as db:
            db.get(Assignment, self.assignment).expires_at = self.now - 1
        with self.assertRaises(HTTPException):
            self.submit()
        self.assertEqual(self.jobs.launches, [])

    def test_reconciler_replicas_serialize_docker_io_for_one_job(self):
        self.arrange()
        from threading import Event
        from concurrent.futures import ThreadPoolExecutor
        from app.job_broker import JobBroker

        self.submit()
        entered, release = Event(), Event()
        original = self.jobs.ensure

        def blocked_launch(launch):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Test release timed out")
            return original(launch)

        self.jobs.ensure = blocked_launch
        other = JobBroker(self.sessions, self.settings, clock=lambda: self.now)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.broker.reconcile_once, self.jobs)
            try:
                self.assertTrue(entered.wait(5))
                self.assertFalse(other.reconcile_once(self.jobs))
            finally:
                release.set()
            self.assertTrue(future.result(timeout=5))
        self.assertEqual(len(self.jobs.launches), 1)

    def set_grant_value(self, value):
        from app.database import SecretVersion
        from app.project_context import encrypt

        with self.sessions.begin() as db:
            secret = db.scalar(
                fixtures.select(SecretVersion).where(
                    SecretVersion.project_id == self.scope.project_id
                )
            )
            secret.ciphertext = encrypt(value, self.settings)

    def test_redaction_preserves_failure_protocol_keys(self):
        self.arrange()
        from app.job_contracts import JobCapture

        self.set_grant_value("error")
        receipt = self.submit()
        self.broker.reconcile_once(self.jobs)
        self.jobs.jobs[receipt.job_id] = "exited"
        self.jobs.collect = lambda ref: JobCapture(
            output={}, error="Nonzero exit", exit_code=1
        )
        self.broker.reconcile_once(self.jobs)
        result = self.submit()
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.result["exit_code"], 1)
        self.assertIsNotNone(result.result["error"])

    def test_redaction_rebounds_logs_and_reports_truncation(self):
        self.arrange()
        from app.job_contracts import JobCapture

        self.set_grant_value("z")
        receipt = self.submit()
        self.broker.reconcile_once(self.jobs)
        self.jobs.jobs[receipt.job_id] = "exited"
        self.jobs.collect = lambda ref: JobCapture(
            output={"ok": True}, stdout="z" * 262144, stderr="z" * 262144
        )
        self.broker.reconcile_once(self.jobs)
        result = self.submit()
        self.assertEqual(result.state, "completed")
        for stream in ("stdout", "stderr"):
            self.assertLessEqual(len(result.result[stream].encode()), 262144)
            self.assertTrue(result.result[stream + "_truncated"])

    def test_oversized_redacted_output_is_bounded_failure(self):
        self.arrange()
        from app.job_contracts import JobCapture

        self.set_grant_value("z")
        receipt = self.submit()
        self.broker.reconcile_once(self.jobs)
        self.jobs.jobs[receipt.job_id] = "exited"
        self.jobs.collect = lambda ref: JobCapture(output={"blob": "z" * 1000000})
        self.broker.reconcile_once(self.jobs)
        result = self.submit()
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.result["output"], {})
        self.assertLessEqual(len(json.dumps(result.result).encode()), 1048576)

    def test_due_history_cannot_delay_launch_or_running_deadline(self):
        self.arrange()
        import uuid
        from app import database as models
        from app.project_context import decrypt, encrypt

        first = self.submit()
        with self.sessions.begin() as db:
            original = db.get(models.RuntimeJob, first.job_id)
            template = decrypt(original.launch_ciphertext, self.settings)
            authority = db.get(models.Assignment, self.assignment)
            for i in range(151):
                run_id, execution_id, assignment_id = [
                    str(uuid.uuid4()) for _ in range(3)
                ]
                job_id = "%064x" % (i + 1)
                run = models.WorkflowRun(
                    id=run_id,
                    project_id=self.scope.project_id,
                    snapshot_id=self.run.snapshot_id,
                    sequence=i + 10,
                    request_key="history-" + str(i),
                    request_digest="a" * 64,
                    input_ciphertext=self.run.input_ciphertext,
                    state="completed",
                )
                assignment = models.Assignment(
                    id=assignment_id,
                    project_id=self.scope.project_id,
                    repository_id=authority.repository_id,
                    pool_id=authority.pool_id,
                    run_id=run_id,
                    stage_id="job",
                    execution_id=execution_id,
                    role="simple",
                    executor="docker@1",
                    action="docker.run@1",
                    status="completed",
                    input_ciphertext=authority.input_ciphertext,
                )
                db.add_all([run, assignment])
                db.flush()
                db.add(
                    models.StageAttempt(
                        id=execution_id,
                        project_id=self.scope.project_id,
                        run_id=run_id,
                        stage_id="job",
                        generation=1,
                        state="completed",
                        assignment_id=assignment_id,
                        spec_digest=self.digest,
                    )
                )
                db.flush()
                document = {
                    **template,
                    "launch": {
                        **template["launch"],
                        "job_id": job_id,
                        "execution_id": execution_id,
                    },
                }
                db.add(
                    models.RuntimeJob(
                        id=job_id,
                        project_id=self.scope.project_id,
                        execution_id=execution_id,
                        assignment_id=assignment_id,
                        spec_digest=self.digest,
                        launch_ciphertext=encrypt(document, self.settings),
                        state="completed",
                        launch_started=True,
                        runtime_observed=True,
                        cancel_requested=False,
                        deadline=self.now - 1000,
                        result_ciphertext=encrypt({}, self.settings),
                        updated_at=self.now - 1000,
                        next_cleanup_at=0,
                    )
                )
        self.broker.reconcile_once(self.jobs)
        self.assertEqual(
            [launch.job_id for launch in self.jobs.launches], [first.job_id]
        )
        self.now = first.deadline + 1
        self.broker.reconcile_once(self.jobs)
        self.assertEqual(self.submit().state, "timed-out")
        self.assertIn(first.job_id, self.jobs.terminated)

    def test_same_key_durable_reattach_and_digest_conflict(self):
        self.arrange()
        first = self.submit()
        self.assertEqual(first, self.submit())
        with self.assertRaises(HTTPException):
            self.submit("c" * 64)
        self.broker.reconcile_once(self.jobs)
        self.broker.reconcile_once(self.jobs)
        self.assertEqual(len(self.jobs.launches), 1)
        self.assertEqual(
            self.jobs.launches[0].credentials, {"openai": "private-job-key"}
        )
        self.assertNotIn("private-job-key", self.jobs.launches[0].model_dump_json())

    def test_recover_after_create_exit_capture_and_terminal_commit_before_cleanup(self):
        self.arrange()
        from app.job_broker import JobBroker

        receipt = self.submit()
        self.jobs.crash = "create"
        self.broker.reconcile_once(self.jobs)
        self.broker = JobBroker(self.sessions, self.settings, clock=lambda: self.now)
        self.broker.reconcile_once(self.jobs)
        self.assertEqual(len(self.jobs.launches), 1)
        self.jobs.jobs[receipt.job_id] = "exited"
        self.jobs.crash = "collect"
        self.broker.reconcile_once(self.jobs)
        self.assertEqual(self.jobs.cleaned, [])
        self.broker.reconcile_once(self.jobs)
        result = self.submit()
        self.assertEqual(result.state, "completed")
        self.assertNotIn("private-job-key", json.dumps(result.result))
        # Terminal receipt is durable before a later cleanup tick.
        self.assertEqual(self.jobs.cleaned, [])
        self.broker.reconcile_once(self.jobs)
        self.assertEqual(self.jobs.cleaned, [receipt.job_id])
        self.assertEqual(self.submit(), result)

    def test_deadline_and_cancellation_do_not_need_live_worker(self):
        self.arrange()
        first = self.submit()
        self.broker.reconcile_once(self.jobs)
        self.now = first.deadline + 1
        self.broker.reconcile_once(self.jobs)
        self.assertEqual(self.submit().state, "timed-out")
        self.assertEqual(self.submit().result["stdout"], "[REDACTED]")
        self.assertEqual(self.jobs.terminated, [first.job_id])

    def test_cancellation_wins_before_terminal_commit_and_foreign_scope_rejected(self):
        self.arrange()
        from app.projects import ProjectScope

        first = self.submit()
        self.broker.reconcile_once(self.jobs)
        self.jobs.jobs[first.job_id] = "exited"
        with self.assertRaises(Exception):
            self.broker.cancel(ProjectScope("foreign"), first.job_id)
        self.broker.cancel(self.scope, first.job_id)
        self.broker.reconcile_once(self.jobs)
        self.assertEqual(self.submit().state, "cancelled")
        self.broker.cancel(self.scope, first.job_id)
        self.assertEqual(self.submit().state, "cancelled")

    def test_new_logical_attempt_gets_new_job_and_pending_cancel_never_launches(self):
        self.arrange()
        from app import database as models

        first = self.submit()
        self.broker.cancel(self.scope, first.job_id)
        self.broker.reconcile_once(self.jobs)
        self.assertEqual(self.submit().state, "cancelled")
        self.assertEqual(self.jobs.launches, [])
        self.store.finish_execution(
            self.scope, self.execution, failed=True, confirmed_terminal=True
        )
        second = self.store.advance_run(self.scope, self.run.id).ready[0]
        with self.sessions.begin() as db:
            attempt = db.get(models.StageAttempt, second.execution_id)
            assignment = db.get(models.Assignment, attempt.assignment_id)
            assignment.status, assignment.worker_id, assignment.expires_at = (
                "running",
                self.jobs_worker(),
                self.now + 60,
            )
            self.assignment, self.execution = assignment.id, second.execution_id
        self.assertNotEqual(self.submit().job_id, first.job_id)

    def jobs_worker(self):
        from app import database as models

        with self.sessions() as db:
            return db.scalar(
                fixtures.select(models.Worker.id).where(
                    models.Worker.protocol == "shared-v1"
                )
            )

    def test_completed_tombstones_do_not_starve_pending_jobs(self):
        self.arrange()
        first = self.submit()
        self.broker.reconcile_once(self.jobs)
        self.jobs.jobs[first.job_id] = "exited"
        self.broker.reconcile_once(self.jobs)
        self.broker.reconcile_once(self.jobs)
        self.assertFalse(self.broker.reconcile_once(self.jobs))

    def test_deadline_crossed_during_capture_cannot_commit_success(self):
        self.arrange()
        receipt = self.submit()
        self.broker.reconcile_once(self.jobs)
        self.jobs.jobs[receipt.job_id] = "exited"
        original = self.jobs.collect

        def cross_deadline(ref):
            self.now = receipt.deadline + 1
            return original(ref)

        self.jobs.collect = cross_deadline
        self.broker.reconcile_once(self.jobs)
        # The artificial clock jump also expires the reconciliation claim. It
        # cannot publish an outcome; the next valid owner confirms termination.
        self.assertNotEqual(self.submit().state, "completed")
        self.broker.reconcile_once(self.jobs)
        self.assertEqual(self.submit().state, "timed-out")

    def test_capture_identity_error_is_not_terminal_proof(self):
        self.arrange()
        receipt = self.submit()
        self.broker.reconcile_once(self.jobs)
        self.jobs.jobs[receipt.job_id] = "exited"

        def wrong_identity(ref):
            raise ValueError("Container identity changed")

        self.jobs.collect = wrong_identity
        self.broker.reconcile_once(self.jobs)
        self.assertNotIn(
            self.submit().state, {"completed", "failed", "cancelled", "timed-out"}
        )
        self.assertEqual(self.jobs.cleaned, [])

    def test_cancel_during_capture_and_failed_commit_keep_container_for_recovery(self):
        self.arrange()
        from unittest.mock import patch

        receipt = self.submit()
        self.broker.reconcile_once(self.jobs)
        self.jobs.jobs[receipt.job_id] = "exited"
        with patch(
            "app.job_broker.encrypt", side_effect=RuntimeError("DB commit lost")
        ):
            self.broker.reconcile_once(self.jobs)
        self.assertNotIn(self.submit().state, {"completed", "failed"})
        self.assertEqual(self.jobs.cleaned, [])
        original = self.jobs.collect

        def cancel_during_capture(ref):
            self.broker.cancel(self.scope, receipt.job_id)
            return original(ref)

        self.jobs.collect = cancel_during_capture
        self.broker.reconcile_once(self.jobs)
        self.assertEqual(self.submit().state, "cancelled")

    def test_uncertain_launch_cannot_report_cancelled_before_effects_settle(self):
        self.arrange()
        receipt = self.submit()

        def lost_launch(launch):
            raise RuntimeError("Request outcome unknown")

        self.jobs.ensure = lost_launch
        self.broker.reconcile_once(self.jobs)
        self.broker.cancel(self.scope, receipt.job_id)
        self.broker.reconcile_once(self.jobs)
        self.assertEqual(self.submit().state, "unknown")
        self.jobs.jobs[receipt.job_id] = "running"
        self.broker.reconcile_once(self.jobs)
        self.assertEqual(self.submit().state, "cancelled")

    def test_missing_started_job_is_unknown_and_never_automatically_recreated(self):
        self.arrange()
        receipt = self.submit()
        self.broker.reconcile_once(self.jobs)
        self.jobs.jobs.pop(receipt.job_id)
        self.broker.reconcile_once(self.jobs)
        self.assertEqual(len(self.jobs.launches), 1)
        self.assertEqual(self.submit().state, "unknown")
        self.broker.cancel(self.scope, receipt.job_id)
        self.broker.reconcile_once(self.jobs)
        self.assertEqual(self.submit().state, "cancelled")
        # Late Docker response after terminal intent is stopped by tombstone reconciliation.
        self.jobs.jobs[receipt.job_id] = "running"
        self.broker.reconcile_once(self.jobs)
        self.assertNotIn(receipt.job_id, self.jobs.jobs)

    @unittest.skipUnless(
        __import__("os")
        .environ.get("CONTROL_TEST_DATABASE_URL", "")
        .startswith("postgresql"),
        "requires independent PostgreSQL connections",
    )
    def test_concurrent_submissions_share_one_persisted_job(self):
        self.arrange()
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(lambda _: self.submit(), range(2)))
        self.assertEqual(receipts[0], receipts[1])
        self.broker.reconcile_once(self.jobs)
        self.assertEqual(len(self.jobs.launches), 1)


class JobMigrationTests(unittest.TestCase):
    def test_upgrade_and_downgrade_job_schema(self):
        import os
        import uuid
        from unittest.mock import patch
        from alembic.config import Config
        from alembic import command
        from sqlalchemy import create_engine, text, inspect
        from sqlalchemy.engine import make_url

        with tempfile.TemporaryDirectory() as tmp:
            url = os.environ.get(
                "CONTROL_MIGRATION_TEST_URL", "sqlite:///" + tmp + "/jobs.db"
            )
            admin = None
            if url.startswith("postgresql"):
                admin = create_engine(url)
                schema = "job_migration_" + uuid.uuid4().hex
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
                    command.upgrade(cfg, "head")
                    engine = create_engine(url)
                    self.assertIn("runtime_jobs", inspect(engine).get_table_names())
                    engine.dispose()
                    command.downgrade(cfg, "e201f1000006")
                    engine = create_engine(url)
                    self.assertNotIn("runtime_jobs", inspect(engine).get_table_names())
                    self.assertIn(
                        "workflow_stage_attempts", inspect(engine).get_table_names()
                    )
                    engine.dispose()
                    command.upgrade(cfg, "head")
            finally:
                if admin:
                    with admin.begin() as db:
                        db.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
                    admin.dispose()
