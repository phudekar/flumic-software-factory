"""Suspended Job intent recovery and UID-bound one-time execution."""

import copy
import importlib.util
import unittest

import test_api as fixtures
import test_runtime_authority as authority_fixtures
import test_docker_jobs as docker_fixtures
from app.job_contracts import JobLaunch, RuntimeJobRef
from app.project_context import decrypt
from kubernetes_protocol_fixture import KubeProtocol


class JobFixture:
    arrange = docker_fixtures.JobBrokerTests.arrange
    submit = docker_fixtures.JobBrokerTests.submit
    kube = authority_fixtures.RuntimeAuthorityTests.kube

    def boot(self):
        fixtures.ControlPlaneTests.setUp(self)
        from app.database import RuntimeJob
        from app.runtime_resources import RuntimeResources
        from app.runtime_claims import claim_runtime, require_runtime_claim
        from app.adapters.kubernetes_api import KubernetesApi

        receipt = self.kube()
        with self.sessions() as db:
            document = decrypt(
                db.get(RuntimeJob, receipt.job_id).launch_ciphertext, self.settings
            )
        self.launch = JobLaunch.model_validate(document["launch"])
        self.ref = RuntimeJobRef(
            **{key: getattr(self.launch, key) for key in RuntimeJobRef.model_fields}
        )
        self.claim = claim_runtime(self.sessions, "job:" + receipt.job_id, self.now)
        self.resources = RuntimeResources(
            self.sessions,
            self.snapshot,
            project_id=self.scope.project_id,
            clock=lambda: self.now,
        )
        self.server = KubeProtocol(self.snapshot.configuration)
        self.api = KubernetesApi(
            self.snapshot,
            clients=self.server.clients,
            effect_guard=lambda: require_runtime_claim(
                self.sessions, self.claim, self.now
            ),
        )

    def tearDown(self):
        if hasattr(self, "client"):
            fixtures.ControlPlaneTests.tearDown(self)


class KubernetesIntentTests(JobFixture, unittest.TestCase):
    def setUp(self):
        self.boot()

    def test_precreate_manifest_is_durable_and_uid_binds_exactly_once(self):
        from app.adapters.kubernetes_job_manifests import job_manifest

        body = job_manifest(self.launch, self.snapshot)
        root = self.resources.prepare_job_root(self.ref.job_id, body, claim=self.claim)
        self.assertIsNone(root.uid)
        with self.assertRaises(ValueError):
            self.resources.require(
                "Job", self.ref.job_id, uid=None, spec_digest=root.spec_digest
            )
        self.assertEqual(root.phase, "intent")
        same = self.resources.prepare_job_root(
            self.ref.job_id, copy.deepcopy(body), claim=self.claim
        )
        self.assertEqual(root.id, same.id)
        bound = self.resources.bind(
            "Job",
            self.ref.job_id,
            namespace=root.namespace,
            name=root.name,
            uid="observed-job-uid",
            spec_digest=root.spec_digest,
            project_id=self.scope.project_id,
            job_id=self.ref.job_id,
            claim=self.claim,
        )
        self.assertEqual((bound.uid, bound.phase), ("observed-job-uid", "prepared"))
        from app.database import RuntimeJob

        with self.sessions() as db:
            self.assertTrue(db.get(RuntimeJob, self.ref.job_id).runtime_observed)
        with self.assertRaises(ValueError):
            self.resources.bind(
                "Job",
                self.ref.job_id,
                namespace=root.namespace,
                name=root.name,
                uid="replacement",
                spec_digest=root.spec_digest,
                project_id=self.scope.project_id,
                job_id=self.ref.job_id,
                claim=self.claim,
            )

    def test_intent_cannot_drift_or_authorize_unobserved_children(self):
        from app.adapters.kubernetes_job_manifests import job_manifest

        body = job_manifest(self.launch, self.snapshot)
        root = self.resources.prepare_job_root(self.ref.job_id, body, claim=self.claim)
        changed = copy.deepcopy(body)
        changed["spec"]["template"]["spec"]["containers"][0]["command"] = [
            "/bin/foreign"
        ]
        with self.assertRaises(ValueError):
            self.resources.prepare_job_root(self.ref.job_id, changed, claim=self.claim)
        with self.assertRaises(ValueError):
            self.resources.record_phase(
                "Job",
                self.ref.job_id,
                phase="running",
                expected_phase="intent",
                claim=self.claim,
            )
        with self.assertRaises(ValueError):
            self.resources.bind(
                "Secret",
                self.ref.job_id + ":unobserved",
                namespace=root.namespace,
                name="unobserved",
                uid="child",
                spec_digest="b" * 64,
                owner_uid=None,
                project_id=self.scope.project_id,
                job_id=self.ref.job_id,
                claim=self.claim,
            )


class KubernetesJobTests(JobFixture, unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(
            importlib.util.find_spec("app.adapters.kubernetes_jobs"),
            "Kubernetes Job runtime is not implemented",
        )
        self.boot()
        from app.adapters.kubernetes_jobs import KubernetesJobRuntime

        self.exec_calls = []

        def execute(name, *, expected_uid, mode):
            self.exec_calls.append((name, expected_uid, mode))
            return (
                {"output": {"answer": 42}, "usage": []} if mode == "capture" else None
            )

        self.api.collector_exec = execute
        self.runtime = KubernetesJobRuntime(
            self.snapshot,
            self.resources,
            api=self.api,
            claim=self.claim,
            clock=lambda: self.now,
        )

    def job(self):
        return next(
            value for key, value in self.server.objects.items() if key[1] == "job"
        )

    def pod(self, *, exit_code=0, collector=True, oom=False):
        job = self.job()
        pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": "owned-pod",
                "namespace": "flumic-actions",
                "uid": "pod-uid",
                "resourceVersion": "1",
                "labels": copy.deepcopy(job["spec"]["template"]["metadata"]["labels"]),
                "annotations": copy.deepcopy(
                    job["spec"]["template"]["metadata"]["annotations"]
                ),
                "ownerReferences": [
                    {
                        "apiVersion": "batch/v1",
                        "kind": "Job",
                        "name": job["metadata"]["name"],
                        "uid": job["metadata"]["uid"],
                        "controller": True,
                    }
                ],
            },
            "spec": copy.deepcopy(job["spec"]["template"]["spec"]),
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {
                        "name": "action",
                        "state": {
                            "terminated": {
                                "exitCode": exit_code,
                                "reason": "OOMKilled" if oom else "Completed",
                            }
                        },
                    }
                ],
            },
        }
        if collector:
            pod["status"]["containerStatuses"].append(
                {"name": "collector", "state": {"running": {"startedAt": "fixture"}}}
            )
        self.server.objects[("core", "pod", "flumic-actions", "owned-pod")] = pod
        return pod

    def captured(self):
        from app.database import RuntimeJob

        with self.sessions.begin() as db:
            row = db.get(RuntimeJob, self.ref.job_id)
            row.state, row.runtime_phase, row.cleanup_pending = (
                "completed",
                "captured",
                True,
            )
        binding = self.resources.get("Job", self.ref.job_id)
        self.resources.record_phase(
            "Job",
            self.ref.job_id,
            phase="captured",
            expected_phase=binding.phase,
            claim=self.claim,
        )

    def test_multiple_live_pods_are_not_reported_dead(self):
        self.runtime.ensure(self.launch)
        first = self.pod()
        first["status"]["containerStatuses"][0]["state"] = {"running": {}}
        second = copy.deepcopy(first)
        second["metadata"].update(name="second-pod", uid="second-uid")
        self.server.objects[("core", "pod", "flumic-actions", "second-pod")] = second
        self.assertNotIn(
            self.runtime.inspect(self.ref).state, {"dead", "exited", "missing"}
        )

    def test_cancellation_waits_for_both_running_pods_to_stop(self):
        from app.database import RuntimeJob
        from app.runtime_claims import release_runtime_claim

        self.runtime.ensure(self.launch)
        first = self.pod()
        first["status"]["containerStatuses"][0]["state"] = {"running": {}}
        second = copy.deepcopy(first)
        second["metadata"].update(name="second-pod", uid="second-uid")
        self.server.objects[("core", "pod", "flumic-actions", "second-pod")] = second
        self.server.clients["batch"].delete_namespaced_job = lambda **kwargs: {
            "status": "Success"
        }
        with self.sessions.begin() as db:
            row = db.get(RuntimeJob, self.ref.job_id)
            row.cancel_requested, row.launch_started = True, True
        release_runtime_claim(self.sessions, self.claim)
        outer = self

        class Router:
            def for_job(self, ref, *, claim):
                from app.adapters.kubernetes_jobs import KubernetesJobRuntime

                return KubernetesJobRuntime(
                    outer.snapshot,
                    outer.resources,
                    api=type(outer.api)(outer.snapshot, clients=outer.server.clients),
                    claim=claim,
                    clock=lambda: outer.now,
                )

        self.broker.reconcile_once(Router())
        with self.sessions() as db:
            self.assertNotIn(
                db.get(RuntimeJob, self.ref.job_id).state,
                {"cancelled", "completed", "timed_out"},
            )
        for pod in (first, second):
            pod["status"]["phase"] = "Failed"
            pod["status"]["containerStatuses"][0]["state"] = {
                "terminated": {"exitCode": 137}
            }
        self.broker.reconcile_once(Router())
        with self.sessions() as db:
            self.assertEqual(db.get(RuntimeJob, self.ref.job_id).state, "cancelled")

    def test_rootless_valid_late_pods_are_bound_and_stopped(self):
        self.runtime.ensure(self.launch)
        first = self.pod()
        self.runtime.inspect(self.ref)
        second = copy.deepcopy(first)
        second["metadata"].update(name="late-pod", uid="late-uid")
        self.server.objects[("core", "pod", "flumic-actions", "late-pod")] = second
        del self.server.objects[
            next(key for key in self.server.objects if key[1] == "job")
        ]
        self.runtime.terminate(self.ref)
        self.assertFalse(any(key[1] == "pod" for key in self.server.objects))

    def test_rootless_termination_refuses_changed_pod_spec(self):
        self.runtime.ensure(self.launch)
        pod = self.pod()
        pod["spec"]["containers"][0]["image"] = "foreign-image"
        key = next(key for key in self.server.objects if key[1] == "job")
        del self.server.objects[key]
        with self.assertRaises(ValueError):
            self.runtime.terminate(self.ref)
        self.assertIn(
            ("core", "pod", "flumic-actions", "owned-pod"), self.server.objects
        )

    def test_cascading_termination_refuses_changed_pod_project(self):
        self.runtime.ensure(self.launch)
        pod = self.pod()
        pod["metadata"]["labels"]["flumic.io/project"] = "foreign-project"
        with self.assertRaises(ValueError):
            self.runtime.terminate(self.ref)
        self.assertTrue(any(key[1] == "job" for key in self.server.objects))
        self.assertIsNone(self.resources.get("Pod", self.ref.job_id + ":pod"))

    def test_rootless_termination_validates_all_before_deleting_any(self):
        self.runtime.ensure(self.launch)
        first = self.pod()
        second = copy.deepcopy(first)
        second["metadata"].update(name="late-pod", uid="late-uid")
        second["metadata"]["labels"]["flumic.io/project"] = "foreign-project"
        self.server.objects[("core", "pod", "flumic-actions", "late-pod")] = second
        del self.server.objects[
            next(key for key in self.server.objects if key[1] == "job")
        ]
        with self.assertRaises(ValueError):
            self.runtime.terminate(self.ref)
        self.assertIn(
            ("core", "pod", "flumic-actions", "owned-pod"), self.server.objects
        )
        self.assertIsNone(self.resources.get("Pod", self.ref.job_id + ":pod"))

    def test_ensure_has_one_uid_payloads_then_unsuspends_with_fixed_budget(self):
        self.assertEqual(self.runtime.ensure(self.launch), self.ref)
        uid = self.job()["metadata"]["uid"]
        self.assertFalse(self.job()["spec"]["suspend"])
        budget = self.job()["spec"]["activeDeadlineSeconds"]
        self.now += 2
        self.assertEqual(self.runtime.ensure(self.launch), self.ref)
        self.assertEqual(self.job()["metadata"]["uid"], uid)
        self.assertEqual(self.job()["spec"]["activeDeadlineSeconds"], budget)
        self.assertEqual(sum(key[1] == "job" for key in self.server.objects), 1)
        payloads = [
            value
            for key, value in self.server.objects.items()
            if key[1] in ("secret", "config_map")
        ]
        self.assertTrue(payloads)
        self.assertTrue(
            all(
                value["metadata"]["ownerReferences"][0]["uid"] == uid
                for value in payloads
            )
        )

    def test_lost_create_reply_recovers_durable_manifest_without_launch_payload(self):
        from app.adapters.kubernetes_api import KubernetesError

        self.server.lose_create = "job"
        with self.assertRaises(KubernetesError):
            self.runtime.ensure(self.launch)
        uid = self.job()["metadata"]["uid"]
        self.assertTrue(self.job()["spec"]["suspend"])
        self.assertEqual(self.runtime.inspect(self.ref).state, "created")
        self.assertEqual(self.resources.get("Job", self.ref.job_id).uid, uid)
        self.runtime.ensure(self.launch)
        self.assertEqual(self.job()["metadata"]["uid"], uid)

    def test_lost_unsuspend_reply_does_not_reset_running_budget(self):
        from app.adapters.kubernetes_api import KubernetesError

        self.server.lose_patch = "job"
        with self.assertRaises(KubernetesError):
            self.runtime.ensure(self.launch)
        budget = self.job()["spec"]["activeDeadlineSeconds"]
        generation = self.job()["metadata"]["generation"]
        self.now += 10
        self.runtime.ensure(self.launch)
        self.assertEqual(self.job()["spec"]["activeDeadlineSeconds"], budget)
        self.assertEqual(self.job()["metadata"]["generation"], generation)

    def test_foreign_uid_and_injected_environment_refuse_effects(self):
        self.runtime.ensure(self.launch)
        original = copy.deepcopy(self.job())
        for changed in ("uid", "env"):
            job = self.job()
            if changed == "uid":
                job["metadata"]["uid"] = "foreign"
            else:
                job["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [
                    {"secretRef": {"name": "foreign"}}
                ]
            before = copy.deepcopy(self.server.objects)
            with self.assertRaises(ValueError):
                self.runtime.inspect(self.ref)
            with self.assertRaises(ValueError):
                self.runtime.terminate(self.ref)
            self.assertEqual(before, self.server.objects)
            self.server.objects[
                ("batch", "job", "flumic-actions", original["metadata"]["name"])
            ] = copy.deepcopy(original)

    def test_zero_budget_never_admits_an_action(self):
        self.now = self.ref.deadline
        with self.assertRaises(ValueError):
            self.runtime.ensure(self.launch)
        self.assertFalse(any(key[1] == "job" for key in self.server.objects))

    def test_action_exit_is_separate_from_running_collector_and_repeatable_capture(
        self,
    ):
        self.runtime.ensure(self.launch)
        self.pod()
        state = self.runtime.inspect(self.ref)
        self.assertEqual((state.state, state.exit_code), ("exited", 0))
        first = self.runtime.collect(self.ref)
        second = self.runtime.collect(self.ref)
        self.assertEqual(first.output, {"answer": 42})
        self.assertEqual(first, second)
        self.assertEqual(first.log_stream, "combined")
        self.assertEqual(first.stderr, "")
        self.assertNotIn("release", [mode for _, _, mode in self.exec_calls])

    def test_missing_or_failed_collector_is_explicit_capture_failure(self):
        self.runtime.ensure(self.launch)
        self.pod(collector=False)
        result = self.runtime.collect(self.ref)
        self.assertTrue(result.error)
        self.assertEqual(result.output, {})
        self.assertFalse(self.exec_calls)

    def test_nonzero_oom_and_failed_pod_cannot_publish_success(self):
        self.runtime.ensure(self.launch)
        for code, oom in ((2, False), (137, True)):
            with self.subTest(code=code):
                self.pod(exit_code=code, oom=oom)
                state = self.runtime.inspect(self.ref)
                self.assertEqual(state.exit_code, code)
                self.assertEqual(state.oom, oom)
                capture = self.runtime.collect(self.ref)
                self.assertEqual(capture.output, {})
                self.assertTrue(capture.error)

    def test_after_hard_deadline_successful_file_is_unavailable(self):
        self.runtime.ensure(self.launch)
        self.pod()
        self.now = self.ref.deadline + 1
        result = self.runtime.collect(self.ref)
        self.assertEqual(result.output, {})
        self.assertTrue(result.error)
        self.assertFalse(self.exec_calls)

    def test_cleanup_and_collector_release_require_canonical_capture(self):
        self.runtime.ensure(self.launch)
        self.pod()
        before = copy.deepcopy(self.server.objects)
        with self.assertRaises(ValueError):
            self.runtime.cleanup(self.ref)
        self.assertEqual(before, self.server.objects)
        self.assertFalse(self.exec_calls)

    def test_previously_observed_missing_job_is_never_recreated(self):
        self.runtime.ensure(self.launch)
        key = next(key for key in self.server.objects if key[1] == "job")
        del self.server.objects[key]
        self.assertEqual(self.runtime.inspect(self.ref).state, "missing")
        with self.assertRaises(ValueError):
            self.runtime.ensure(self.launch)
        self.assertFalse(any(key[1] == "job" for key in self.server.objects))

    def test_reserved_unobserved_intent_is_retryable_without_claiming_remote_observation(
        self,
    ):
        from app.adapters.kubernetes_job_manifests import job_manifest

        self.resources.prepare_job_root(
            self.ref.job_id, job_manifest(self.launch, self.snapshot), claim=self.claim
        )
        self.assertEqual(self.runtime.inspect(self.ref).state, "intent")
        self.assertIsNone(self.resources.get("Job", self.ref.job_id).uid)
        self.runtime.ensure(self.launch)
        self.assertIsNotNone(self.resources.get("Job", self.ref.job_id).uid)

    def test_broker_retries_reserved_create_and_does_not_mark_missing_intent_observed(
        self,
    ):
        from app.database import RuntimeJob
        from app.adapters.kubernetes_job_manifests import job_manifest
        from app.runtime_claims import release_runtime_claim

        self.resources.prepare_job_root(
            self.ref.job_id, job_manifest(self.launch, self.snapshot), claim=self.claim
        )
        with self.sessions.begin() as db:
            db.get(RuntimeJob, self.ref.job_id).launch_started = True
        release_runtime_claim(self.sessions, self.claim)
        outer = self

        class Router:
            def for_job(self, ref, *, claim):
                from app.adapters.kubernetes_jobs import KubernetesJobRuntime

                return KubernetesJobRuntime(
                    outer.snapshot,
                    outer.resources,
                    api=type(outer.api)(outer.snapshot, clients=outer.server.clients),
                    claim=claim,
                    clock=lambda: outer.now,
                )

        self.broker.reconcile_once(Router())
        with self.sessions() as db:
            row = db.get(RuntimeJob, self.ref.job_id)
            self.assertTrue(row.runtime_observed)
            self.assertEqual(row.state, "running")
        self.assertFalse(self.job()["spec"]["suspend"])

    def test_cleanup_never_executes_a_replaced_collector_image(self):
        self.runtime.ensure(self.launch)
        pod = self.pod()
        self.runtime.inspect(self.ref)
        self.captured()
        pod["spec"]["containers"][1]["image"] = "foreign/collector@sha256:" + "b" * 64
        with self.assertRaises(ValueError):
            self.runtime.cleanup(self.ref)
        self.assertFalse(self.exec_calls)

    def test_cancel_before_delayed_root_create_stays_suspended_and_is_reconciled(self):
        from app.database import RuntimeJob
        from app.adapters.kubernetes_job_manifests import job_manifest

        self.resources.prepare_job_root(
            self.ref.job_id, job_manifest(self.launch, self.snapshot), claim=self.claim
        )
        with self.sessions.begin() as db:
            db.get(RuntimeJob, self.ref.job_id).cancel_requested = True
        self.assertEqual(self.runtime.inspect(self.ref).state, "missing")
        with self.assertRaises(ValueError):
            self.runtime.ensure(self.launch)
        self.server.clients["batch"].create_namespaced_job(
            namespace="flumic-actions", body=job_manifest(self.launch, self.snapshot)
        )
        self.runtime.terminate(self.ref)
        self.assertEqual(self.runtime.inspect(self.ref).state, "missing")

    def test_canonical_capture_may_commit_without_assigning_a_fabricated_uid(self):
        from app.database import RuntimeJob
        from app.adapters.kubernetes_job_manifests import job_manifest
        from app.runtime_claims import release_runtime_claim

        self.resources.prepare_job_root(
            self.ref.job_id, job_manifest(self.launch, self.snapshot), claim=self.claim
        )
        with self.sessions.begin() as db:
            db.get(RuntimeJob, self.ref.job_id).cancel_requested = True
            db.get(RuntimeJob, self.ref.job_id).launch_started = True
        release_runtime_claim(self.sessions, self.claim)
        outer = self

        class Router:
            def for_job(self, ref, *, claim):
                from app.adapters.kubernetes_jobs import KubernetesJobRuntime

                return KubernetesJobRuntime(
                    outer.snapshot,
                    outer.resources,
                    api=type(outer.api)(outer.snapshot, clients=outer.server.clients),
                    claim=claim,
                    clock=lambda: outer.now,
                )

        self.broker.reconcile_once(Router())
        with self.sessions() as db:
            row = db.get(RuntimeJob, self.ref.job_id)
            self.assertEqual(row.state, "cancelled")
            self.assertEqual(row.runtime_phase, "captured")
        self.assertIsNone(self.resources.get("Job", self.ref.job_id).uid)

    def test_log_transport_outage_does_not_discard_available_result(self):
        from app.adapters.kubernetes_api import KubernetesError

        self.runtime.ensure(self.launch)
        self.pod()

        def unavailable(*args, **kwargs):
            raise KubernetesError(503)

        self.api.pod_logs = unavailable
        result = self.runtime.collect(self.ref)
        self.assertEqual(result.output, {"answer": 42})
        self.assertTrue(result.stdout_truncated)

    def test_lost_collect_reply_can_be_retried_without_releasing_collector(self):
        from app.adapters.kubernetes_api import KubernetesError

        self.runtime.ensure(self.launch)
        self.pod()
        execute = self.api.collector_exec

        def lost(*args, **kwargs):
            self.api.collector_exec = execute
            raise KubernetesError(503)

        self.api.collector_exec = lost
        with self.assertRaises(KubernetesError):
            self.runtime.collect(self.ref)
        self.assertEqual(self.runtime.collect(self.ref).output, {"answer": 42})
        self.assertNotIn("release", [mode for _, _, mode in self.exec_calls])

    def test_lost_delete_reply_recovers_missing_uid_without_recreating(self):
        from app.adapters.kubernetes_api import KubernetesError

        self.runtime.ensure(self.launch)
        self.server.lose_delete = "job"
        with self.assertRaises(KubernetesError):
            self.runtime.terminate(self.ref)
        self.assertEqual(self.runtime.inspect(self.ref).state, "missing")
        self.runtime.terminate(self.ref)
        self.assertFalse(any(key[1] == "job" for key in self.server.objects))

    def test_stale_claim_cannot_publish_running_phase_after_api_return(self):
        from app.database import RuntimeLease, RuntimeJob

        original = self.server.clients["batch"].patch_namespaced_job

        def changed(**kwargs):
            result = original(**kwargs)
            with self.sessions.begin() as db:
                db.get(RuntimeLease, "job:" + self.ref.job_id).epoch += 1
            return result

        self.server.clients["batch"].patch_namespaced_job = changed
        with self.assertRaises(ValueError):
            self.runtime.ensure(self.launch)
        self.assertEqual(
            self.resources.get("Job", self.ref.job_id).phase, "payload-ready"
        )
        with self.sessions() as db:
            self.assertEqual(db.get(RuntimeJob, self.ref.job_id).state, "pending")

    def test_retry_fences_delayed_unsuspend_before_replacing_budget(self):
        from app.adapters.kubernetes_api import KubernetesApi, KubernetesError
        from app.adapters.kubernetes_jobs import KubernetesJobRuntime
        from app.runtime_claims import claim_runtime

        original = self.server.clients["batch"].patch_namespaced_job
        old_patch = []

        def hold(**kwargs):
            if any(
                p.get("path") == "/spec/suspend" and p.get("value") is False
                for p in kwargs["body"]
            ):
                old_patch.append(copy.deepcopy(kwargs))
                raise KubernetesError("Delayed request")
            return original(**kwargs)

        self.server.clients["batch"].patch_namespaced_job = hold
        with self.assertRaises(KubernetesError):
            self.runtime.ensure(self.launch)
        self.now += 61
        claim = claim_runtime(self.sessions, "job:" + self.ref.job_id, self.now)
        runtime = KubernetesJobRuntime(
            self.snapshot,
            self.resources,
            api=KubernetesApi(self.snapshot, clients=self.server.clients),
            claim=claim,
            clock=lambda: self.now,
        )
        self.server.clients["batch"].patch_namespaced_job = original
        prepare = self.resources.prepare_job_start
        delayed_rejected = []

        def interleave(*args, **kwargs):
            result = prepare(*args, **kwargs)
            try:
                original(**old_patch[0])
            except Exception as error:
                if getattr(error, "status", None) != 409:
                    raise
                delayed_rejected.append(True)
            return result

        self.resources.prepare_job_start = interleave
        runtime.ensure(self.launch)
        self.assertEqual(delayed_rejected, [True])
        self.assertEqual(runtime.inspect(self.ref).state, "running")

    def test_lost_fence_reply_recovers_only_approved_metadata(self):
        from app.adapters.kubernetes_api import KubernetesError

        original = self.server.clients["batch"].patch_namespaced_job

        def hold(**kwargs):
            if any(
                p.get("path") == "/spec/suspend" and p.get("value") is False
                for p in kwargs["body"]
            ):
                raise KubernetesError(503)
            return original(**kwargs)

        self.server.clients["batch"].patch_namespaced_job = hold
        with self.assertRaises(KubernetesError):
            self.runtime.ensure(self.launch)
        self.server.clients["batch"].patch_namespaced_job = original
        self.server.lose_patch = "job"
        with self.assertRaises(KubernetesError):
            self.runtime.ensure(self.launch)
        self.runtime.ensure(self.launch)
        self.assertEqual(self.runtime.inspect(self.ref).state, "running")
        self.job()["metadata"]["annotations"]["flumic.io/start-fence"] = "foreign-fence"
        with self.assertRaises(ValueError):
            self.runtime.inspect(self.ref)

    def test_cancellation_fences_delayed_unsuspend_when_payload_blocks_delete(self):
        from app.adapters.kubernetes_api import KubernetesError

        original = self.server.clients["batch"].patch_namespaced_job
        delayed = []

        def hold(**kwargs):
            if any(
                p.get("path") == "/spec/suspend" and p.get("value") is False
                for p in kwargs["body"]
            ):
                delayed.append(copy.deepcopy(kwargs))
                raise KubernetesError(503)
            return original(**kwargs)

        self.server.clients["batch"].patch_namespaced_job = hold
        with self.assertRaises(KubernetesError):
            self.runtime.ensure(self.launch)
        secret = next(
            value for key, value in self.server.objects.items() if key[1] == "secret"
        )
        secret["metadata"]["uid"] = "foreign-secret"
        self.server.clients["batch"].patch_namespaced_job = original
        self.runtime.terminate(self.ref)
        with self.assertRaises(Exception) as caught:
            original(**delayed[0])
        self.assertEqual(getattr(caught.exception, "status", None), 409)
        self.assertEqual(self.runtime.inspect(self.ref).state, "dead")

    def test_new_owner_cancellation_fences_delayed_unsuspend(self):
        from app.adapters.kubernetes_api import KubernetesApi, KubernetesError
        from app.adapters.kubernetes_jobs import KubernetesJobRuntime
        from app.runtime_claims import claim_runtime
        from app.database import RuntimeJob

        original = self.server.clients["batch"].patch_namespaced_job

        def delayed(**kwargs):
            if not any(
                item.get("path") == "/spec/suspend" and item.get("value") is False
                for item in kwargs["body"]
            ):
                return original(**kwargs)
            self.now += 61
            with self.sessions.begin() as db:
                db.get(RuntimeJob, self.ref.job_id).cancel_requested = True
            claim = claim_runtime(self.sessions, "job:" + self.ref.job_id, self.now)
            runtime = KubernetesJobRuntime(
                self.snapshot,
                self.resources,
                api=KubernetesApi(self.snapshot, clients=self.server.clients),
                claim=claim,
                clock=lambda: self.now,
            )
            runtime.terminate(self.ref)
            return original(**kwargs)

        self.server.clients["batch"].patch_namespaced_job = delayed
        with self.assertRaises(KubernetesError):
            self.runtime.ensure(self.launch)
        self.assertFalse(any(key[1] == "job" for key in self.server.objects))
        with self.sessions() as db:
            row = db.get(RuntimeJob, self.ref.job_id)
            self.assertTrue(row.cancel_requested)
            self.assertNotEqual(row.state, "completed")

    def test_broker_capture_is_redacted_committed_then_released_and_cleanup_recovers(
        self,
    ):
        from app.database import RuntimeJob
        from app.runtime_claims import release_runtime_claim
        from app.adapters.kubernetes_api import KubernetesApi
        from app.adapters.kubernetes_jobs import KubernetesJobRuntime
        from app.project_context import decrypt

        release_runtime_claim(self.sessions, self.claim)
        outer = self

        class Router:
            def for_job(self, ref, *, claim):
                api = KubernetesApi(outer.snapshot, clients=outer.server.clients)

                def execute(name, *, expected_uid, mode):
                    outer.exec_calls.append((name, expected_uid, mode))
                    if mode == "release":
                        with outer.sessions() as db:
                            row = db.get(RuntimeJob, ref.job_id)
                            outer.assertEqual(row.state, "completed")
                            outer.assertIsNotNone(row.result_ciphertext)
                        return None
                    return {"output": {"echo": "private-job-key"}, "usage": []}

                api.collector_exec = execute
                return KubernetesJobRuntime(
                    outer.snapshot,
                    outer.resources,
                    api=api,
                    claim=claim,
                    clock=lambda: outer.now,
                )

        self.broker.reconcile_once(Router())
        self.pod()
        self.broker.reconcile_once(Router())
        with self.sessions() as db:
            row = db.get(RuntimeJob, self.ref.job_id)
            self.assertEqual(row.state, "completed")
            self.assertEqual(
                decrypt(row.result_ciphertext, self.settings)["output"],
                {"echo": "[REDACTED]"},
            )
        self.assertNotIn("release", [mode for _, _, mode in self.exec_calls])
        self.broker.reconcile_once(Router())
        self.assertIn("release", [mode for _, _, mode in self.exec_calls])
        with self.sessions() as db:
            self.assertTrue(db.get(RuntimeJob, self.ref.job_id).cleanup_pending)
        # Simulate the asynchronous Job controller completing foreground Pod GC.
        del self.server.objects[("core", "pod", "flumic-actions", "owned-pod")]
        self.broker.reconcile_once(Router())
        with self.sessions() as db:
            row = db.get(RuntimeJob, self.ref.job_id)
            self.assertFalse(row.cleanup_pending)
            self.assertEqual(row.runtime_phase, "removed")
            self.assertIsNone(row.error)
        self.assertFalse(self.server.objects)

    def test_pending_scheduling_and_image_failure_are_not_action_success(self):
        self.runtime.ensure(self.launch)
        pod = self.pod()
        for reason in ("ImagePullBackOff", "Unschedulable"):
            pod["status"] = {
                "phase": "Pending",
                "containerStatuses": [
                    {"name": "action", "state": {"waiting": {"reason": reason}}}
                ],
                "conditions": [
                    {"type": "PodScheduled", "status": "False", "reason": reason}
                ],
            }
            state = self.runtime.inspect(self.ref)
            self.assertIn(state.state, {"created", "running"})
            self.assertIsNone(state.exit_code)
        pod["status"] = {
            "phase": "Failed",
            "reason": "Evicted",
            "containerStatuses": [],
        }
        self.assertEqual(self.runtime.inspect(self.ref).state, "dead")
        self.assertTrue(self.runtime.collect(self.ref).error)

    def test_foreign_pod_owner_prevents_log_and_result_access(self):
        self.runtime.ensure(self.launch)
        pod = self.pod()
        pod["metadata"]["ownerReferences"][0]["uid"] = "foreign-job"
        with self.assertRaises(ValueError):
            self.runtime.inspect(self.ref)
        with self.assertRaises(ValueError):
            self.runtime.collect(self.ref)
        self.assertFalse(self.exec_calls)
        self.assertFalse(any(call[0] == "logs" for call in self.server.calls))

    def test_malformed_usage_is_failure_not_raw_result_publication(self):
        self.runtime.ensure(self.launch)
        self.pod()
        self.api.collector_exec = lambda *args, **kwargs: {
            "output": {"answer": 42},
            "usage": [{"tokens": -1, "provider": "private raw message"}],
        }
        result = self.runtime.collect(self.ref)
        self.assertEqual(result.output, {})
        self.assertTrue(result.error)
        self.assertNotIn("private raw message", result.error)

    def test_cleanup_validates_payload_uid_before_cascading_job_delete(self):
        self.runtime.ensure(self.launch)
        self.pod()
        self.runtime.inspect(self.ref)
        self.captured()
        secret = next(
            value for key, value in self.server.objects.items() if key[1] == "secret"
        )
        secret["metadata"]["uid"] = "foreign-payload"
        with self.assertRaises(ValueError):
            self.runtime.cleanup(self.ref)
        self.assertTrue(any(key[1] == "job" for key in self.server.objects))
        self.assertFalse(self.exec_calls)

    def test_termination_suspends_instead_of_cascading_through_changed_payload_uid(
        self,
    ):
        self.runtime.ensure(self.launch)
        secret = next(
            value for key, value in self.server.objects.items() if key[1] == "secret"
        )
        secret["metadata"]["uid"] = "foreign-payload"
        self.runtime.terminate(self.ref)
        self.assertTrue(self.job()["spec"]["suspend"])
        self.assertEqual(secret["metadata"]["uid"], "foreign-payload")
        self.assertEqual(self.runtime.inspect(self.ref).state, "dead")

    def test_foreground_deletion_bookkeeping_preserves_job_identity(self):
        self.runtime.ensure(self.launch)
        self.job()["metadata"].update(
            deletionTimestamp="2026-10-27T00:00:00Z", finalizers=["foregroundDeletion"]
        )
        self.assertEqual(self.runtime.inspect(self.ref).state, "removing")

    def test_collector_response_crossing_deadline_is_never_accepted(self):
        self.runtime.ensure(self.launch)
        self.pod()
        execute = self.api.collector_exec

        def delayed(*args, **kwargs):
            result = execute(*args, **kwargs)
            self.now = self.ref.deadline + 1
            return result

        self.api.collector_exec = delayed
        result = self.runtime.collect(self.ref)
        self.assertEqual(result.output, {})
        self.assertTrue(result.error)

    def test_unreserved_same_name_foreign_job_is_never_adopted_or_deleted(self):
        from app.adapters.kubernetes_job_manifests import job_manifest

        body = job_manifest(self.launch, self.snapshot)
        body["metadata"]["ownerReferences"] = [
            {
                "kind": "Job",
                "apiVersion": "batch/v1",
                "uid": "foreign",
                "name": "foreign",
            }
        ]
        self.server.clients["batch"].create_namespaced_job(
            namespace="flumic-actions", body=body
        )
        before = copy.deepcopy(self.server.objects)
        with self.assertRaises(ValueError):
            self.runtime.ensure(self.launch)
        with self.assertRaises(ValueError):
            self.runtime.terminate(self.ref)
        self.assertEqual(before, self.server.objects)
        self.assertIsNone(self.resources.get("Job", self.ref.job_id).uid)

    def test_status_transport_failure_does_not_prove_action_termination(self):
        from app.adapters.kubernetes_api import KubernetesError

        self.runtime.ensure(self.launch)

        def unavailable(**kwargs):
            raise KubernetesError(503)

        self.server.clients["batch"].read_namespaced_job = unavailable
        with self.assertRaises(KubernetesError):
            self.runtime.terminate(self.ref)
        with self.assertRaises(KubernetesError):
            self.runtime.inspect(self.ref)
        self.assertTrue(any(key[1] == "job" for key in self.server.objects))

    def test_failed_job_without_pod_and_crashed_collector_are_capture_failures(self):
        self.runtime.ensure(self.launch)
        self.job()["status"] = {
            "conditions": [
                {"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}
            ]
        }
        self.assertEqual(self.runtime.inspect(self.ref).state, "dead")
        self.assertTrue(self.runtime.collect(self.ref).error)
        pod = self.pod()
        pod["status"]["containerStatuses"][1]["state"] = {"terminated": {"exitCode": 1}}
        self.assertTrue(self.runtime.collect(self.ref).error)
        self.assertFalse(self.exec_calls)

    def test_owned_pod_is_stopped_even_if_job_object_already_disappeared(self):
        self.runtime.ensure(self.launch)
        pod = self.pod()
        pod["status"]["containerStatuses"][0]["state"] = {
            "running": {"startedAt": "fixture"}
        }
        self.runtime.inspect(self.ref)
        del self.server.objects[
            next(key for key in self.server.objects if key[1] == "job")
        ]
        self.assertEqual(self.runtime.inspect(self.ref).state, "removing")
        self.runtime.terminate(self.ref)
        self.assertFalse(any(key[1] == "pod" for key in self.server.objects))
        self.assertEqual(self.runtime.inspect(self.ref).state, "missing")


class KubernetesExecTests(unittest.TestCase):
    def setUp(self):
        from app.adapters.kubernetes_api import KubernetesApi
        from test_kubernetes_job_payload import snapshot

        self.snapshot = snapshot()
        self.server = KubeProtocol(self.snapshot.configuration)
        self.server.objects[("core", "pod", "flumic-actions", "owned-pod")] = {
            "metadata": {"name": "owned-pod", "uid": "pod-uid"}
        }
        self.api = KubernetesApi(
            self.snapshot, clients=self.server.clients, effect_guard=lambda: None
        )

    def execute(self, raw, *, code=0):
        from contextlib import contextmanager
        from types import SimpleNamespace
        from unittest.mock import patch

        class Stream:
            def __init__(self):
                self.open = True
                self.buffer = ""
                self.chunks = [raw]
                self.closed = False
                self.socket_closed = False
                self.sock = SimpleNamespace(shutdown=self.shutdown_socket)
                self.returncode = code

            def shutdown_socket(self):
                self.socket_closed = True

            def is_open(self):
                return self.open

            def update(self, timeout=0):
                if not self.buffer:
                    if self.chunks:
                        self.buffer = self.chunks.pop(0)
                    else:
                        self.open = False

            def peek_stdout(self, timeout=0):
                return self.buffer

            def read_stdout(self, timeout=0):
                value = self.buffer
                self.buffer = ""
                return value

            def peek_stderr(self, timeout=0):
                return ""

            def close(self):
                self.closed = True
                self.open = False

        response = Stream()

        @contextmanager
        def separate():
            yield SimpleNamespace(connect_get_namespaced_pod_exec=object())

        with (
            patch.object(self.api, "exec_client", separate),
            patch("kubernetes.stream.stream", return_value=response) as call,
        ):
            try:
                result = self.api.collector_exec(
                    "owned-pod", expected_uid="pod-uid", mode="capture"
                )
            finally:
                self.assertTrue(response.closed)
                self.assertTrue(response.socket_closed)
            self.assertEqual(call.call_args.kwargs["namespace"], "flumic-actions")
            self.assertEqual(call.call_args.kwargs["container"], "collector")
            self.assertEqual(
                call.call_args.kwargs["command"],
                ["python", "-I", "-B", "/opt/flumic/flumic_job_helper.py", "capture"],
            )
            self.assertEqual(
                self.api.call(
                    "core",
                    "read_namespaced_pod",
                    namespace_kind="job",
                    name="owned-pod",
                )["metadata"]["uid"],
                "pod-uid",
            )
            return result

    def test_bounded_exec_uses_separate_transport_and_rest_still_works(self):
        self.assertEqual(
            self.execute('{"output":{"ok":true},"usage":[]}')["output"], {"ok": True}
        )

    def test_exec_rejects_oversized_nonfinite_malformed_and_nonzero_responses(self):
        for raw, code in (
            ("x" * 1048577, 0),
            ('{"output":{"n":NaN}}', 0),
            ("not-json", 0),
            ('{"output":{}}', 1),
        ):
            with (
                self.subTest(code=code, length=len(raw)),
                self.assertRaises(ValueError),
            ):
                self.execute(raw, code=code)

    def test_exec_refuses_arbitrary_command_or_foreign_pod(self):
        with self.assertRaises(ValueError):
            self.api.collector_exec("owned-pod", expected_uid="pod-uid", mode="shell")
        with self.assertRaises(ValueError):
            self.api.collector_exec("owned-pod", expected_uid="foreign", mode="capture")

    def test_exec_deadline_is_bounded_and_always_closes_stream(self):
        from unittest.mock import patch

        with (
            patch("app.adapters.kubernetes_api.time.monotonic", side_effect=[0, 16]),
            self.assertRaises(ValueError),
        ):
            self.execute('{"output":{},"usage":[]}')
