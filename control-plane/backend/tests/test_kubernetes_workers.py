"""Kubernetes workers must retain scoped identity across retries and restarts."""

import copy
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import unittest
import tempfile

import test_api as fixtures
from test_runtime_targets import target
from kubernetes_protocol_fixture import KubeProtocol


def require_boundary(test):
    test.assertIsNotNone(
        importlib.util.find_spec("app.adapters.kubernetes_api"),
        "Scoped Kubernetes API boundary is not implemented",
    )
    test.assertIsNotNone(
        importlib.util.find_spec("app.adapters.kubernetes_workers"),
        "Kubernetes worker runtime is not implemented",
    )


class KubernetesApiTests(unittest.TestCase):
    def setUp(self):
        require_boundary(self)
        from app.runtime_targets import KubernetesTarget, TargetSnapshot, digest

        document = KubernetesTarget.model_validate(target()).model_dump(mode="json")
        self.target = TargetSnapshot(
            id=document["id"],
            kind="kubernetes",
            configuration=document,
            revision=digest(document),
        )
        self.server = KubeProtocol(document)

    def test_explicit_context_does_not_change_global_sdk_configuration(self):
        from kubernetes import client
        from app.adapters.kubernetes_api import KubernetesApi
        from app.runtime_targets import KubernetesTarget, TargetSnapshot, digest

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = {
                "apiVersion": "v1",
                "kind": "Config",
                "current-context": "other",
                "clusters": [
                    {
                        "name": "selected",
                        "cluster": {"server": "https://selected.invalid"},
                    },
                    {"name": "other", "cluster": {"server": "https://ambient.invalid"}},
                ],
                "contexts": [
                    {
                        "name": "explicit",
                        "context": {"cluster": "selected", "user": "fixture"},
                    },
                    {
                        "name": "other",
                        "context": {"cluster": "other", "user": "fixture"},
                    },
                ],
                "users": [
                    {"name": "fixture", "user": {"token": "fixture-transport-secret"}}
                ],
            }
            path.write_text(json.dumps(config))
            document = target()
            document["auth"] = {
                "mode": "kubeconfig",
                "kubeconfig": str(path),
                "context": "explicit",
            }
            document = KubernetesTarget.model_validate(document).model_dump(mode="json")
            snapshot = TargetSnapshot(
                id=document["id"],
                kind="kubernetes",
                configuration=document,
                revision=digest(document),
            )
            before = client.Configuration.get_default_copy().host
            api = KubernetesApi(snapshot)
            self.addCleanup(api.close)
            self.assertEqual(api.configuration.host, "https://selected.invalid")
            self.assertEqual(client.Configuration.get_default_copy().host, before)
            with api.exec_client() as exec_core:
                self.assertIsNot(exec_core.api_client, api.api_client)
                self.assertEqual(
                    exec_core.api_client.configuration.host, api.configuration.host
                )
            self.assertEqual(
                api.api_client.configuration.host, "https://selected.invalid"
            )
            config["clusters"][0]["cluster"]["insecure-skip-tls-verify"] = True
            path.write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                KubernetesApi(snapshot)

    def test_namespace_replacement_stops_effects_and_errors_hide_server_content(self):
        from app.adapters.kubernetes_api import KubernetesApi

        api = KubernetesApi(
            self.target, clients=self.server.clients, effect_guard=lambda: None
        )
        self.server.namespace_uids["flumic-workers"] = "replacement"
        with self.assertRaises(ValueError):
            api.call(
                "core",
                "create_namespaced_secret",
                namespace_kind="worker",
                body={"metadata": {"name": "test"}},
            )
        self.assertEqual(self.server.objects, {})
        self.server.namespace_uids["flumic-workers"] = self.target.configuration[
            "worker_namespace_uid"
        ]
        with self.assertRaises(RuntimeError) as error:
            api.call(
                "apps",
                "read_namespaced_deployment",
                namespace_kind="worker",
                name="missing",
            )
        self.assertNotIn("private-server-secret", str(error.exception))
        self.assertEqual(getattr(error.exception, "status", None), 404)

    def test_read_only_client_never_acquires_mutation_authority(self):
        from app.adapters.kubernetes_api import KubernetesApi

        api = KubernetesApi(self.target, clients=self.server.clients)
        api.require_namespace("worker")
        with self.assertRaises(ValueError):
            api.call(
                "core",
                "create_namespaced_secret",
                namespace_kind="worker",
                body={"metadata": {"name": "test"}},
            )
        self.assertEqual(self.server.objects, {})

    def test_missing_admission_fence_refuses_real_mutation(self):
        from app.adapters.kubernetes_api import KubernetesApi

        api = KubernetesApi(
            self.target, clients=self.server.clients, effect_guard=lambda: None
        )
        self.server.enforce_fence = False
        with self.assertRaises(ValueError):
            api.call(
                "core",
                "create_namespaced_secret",
                namespace_kind="worker",
                body={
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": "test"},
                },
            )
        self.assertEqual(self.server.objects, {})

    def test_missing_namespace_uid_bootstrap_annotation_refuses_effects(self):
        from app.adapters.kubernetes_api import KubernetesApi

        api = KubernetesApi(
            self.target, clients=self.server.clients, effect_guard=lambda: None
        )
        self.server.namespace_fences["flumic-workers"] = None
        with self.assertRaises(ValueError):
            api.call(
                "core",
                "create_namespaced_secret",
                namespace_kind="worker",
                body={
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": "test"},
                },
            )
        self.assertEqual(self.server.objects, {})

    def test_patch_and_delete_require_remote_identity_preconditions(self):
        from app.adapters.kubernetes_api import KubernetesApi

        api = KubernetesApi(
            self.target, clients=self.server.clients, effect_guard=lambda: None
        )
        for method, body in (
            (
                "patch_namespaced_deployment",
                [{"op": "replace", "path": "/spec/replicas", "value": 1}],
            ),
            ("delete_namespaced_deployment", {}),
        ):
            with self.subTest(method=method), self.assertRaises(ValueError):
                api.call(
                    "apps", method, namespace_kind="worker", name="unknown", body=body
                )

    def test_pod_logs_are_bounded_and_cannot_override_namespace(self):
        from app.adapters.kubernetes_api import KubernetesApi

        api = KubernetesApi(self.target, clients=self.server.clients)
        self.server.log_bytes = b"x" * 400000
        self.server.objects[("core", "pod", "flumic-workers", "pod")] = {
            "metadata": {"name": "pod", "uid": "pod-uid"}
        }
        value, truncated = api.pod_logs(
            "pod",
            namespace_kind="worker",
            container="worker",
            limit=100,
            expected_uid="pod-uid",
        )
        self.assertEqual(value, "x" * 100)
        self.assertTrue(truncated)
        with self.assertRaises(ValueError):
            api.call(
                "core",
                "list_namespaced_pod",
                namespace_kind="worker",
                namespace="another-project",
            )

    def test_replaced_log_pod_cannot_return_foreign_data(self):
        import io
        from app.adapters.kubernetes_api import KubernetesApi

        key = ("core", "pod", "flumic-workers", "pod")
        self.server.objects[key] = {"metadata": {"name": "pod", "uid": "owned-uid"}}

        def replace(**kwargs):
            self.server.objects[key]["metadata"]["uid"] = "foreign-uid"
            return io.BytesIO(b"private foreign log")

        self.server.clients["core"].read_namespaced_pod_log = replace
        api = KubernetesApi(self.target, clients=self.server.clients)
        with self.assertRaises(ValueError):
            api.pod_logs(
                "pod",
                namespace_kind="worker",
                container="worker",
                expected_uid="owned-uid",
            )

    def test_long_operator_deployment_id_still_produces_valid_label_values(self):
        from types import SimpleNamespace
        from app.adapters.kubernetes_api import KubernetesApi
        from app.adapters.kubernetes_workers import KubernetesWorkerRuntime
        from app.runtime_targets import KubernetesTarget, TargetSnapshot, digest

        document = target()
        document["deployment_id"] = "f" * 80
        document = KubernetesTarget.model_validate(document).model_dump(mode="json")
        pin = TargetSnapshot(
            id=document["id"],
            kind="kubernetes",
            configuration=document,
            revision=digest(document),
        )
        worker = SimpleNamespace(
            id="11111111-1111-4111-8111-111111111111",
            pool_id="fixture-pool",
            protocol="shared-v1",
            target_id=pin.id,
            target_snapshot=pin.model_dump(),
        )
        adapter = KubernetesWorkerRuntime(
            SimpleNamespace(root=fixtures.ROOT),
            None,
            pin,
            SimpleNamespace(snapshot=pin),
            api=KubernetesApi(pin, clients={}),
        )
        body = adapter._bodies(
            worker,
            {
                "protocol": "shared-v1",
                "worker_id": worker.id,
                "pool_id": worker.pool_id,
                "role": "simple",
            },
            "fixture-token",
        )[0]
        self.assertTrue(
            all(len(value) <= 63 for value in body["metadata"]["labels"].values())
        )


class KubernetesWorkerTests(unittest.TestCase):
    def setUp(self):
        require_boundary(self)
        fixtures.ControlPlaneTests.setUp(self)
        from app.runtime_targets import TargetRegistry
        from app.runtime_claims import claim_runtime, require_runtime_claim
        from app.runtime_resources import RuntimeResources
        from app.database import Worker, WorkerPool
        from app.worker_pools import shared_profile
        from app.adapters.kubernetes_api import KubernetesApi
        from app.adapters.kubernetes_workers import KubernetesWorkerRuntime

        path = Path(self.temporary.name) / "targets.json"
        path.write_text(json.dumps({"schema_version": 1, "targets": [target()]}))
        self.settings = replace(self.settings, runtime_targets_file=path)
        self.snapshot = TargetRegistry(self.settings).snapshot("test-kubernetes")
        pool = self.client.post(
            "/api/v1/worker-pools", json={"name": "kube-fixture", "role": "simple"}
        ).json()["id"]
        worker_id = self.client.post(
            "/api/v1/worker-pools/" + pool + "/workers",
            json={"name": "kube-fixture-worker"},
        ).json()["id"]
        with self.sessions.begin() as db:
            self.worker = db.get(Worker, worker_id)
            self.worker.target_id = self.snapshot.id
            self.worker.target_snapshot = self.snapshot.model_dump()
            self.document = shared_profile(self.worker, db.get(WorkerPool, pool))
        self.now = 1000.0
        self.claim = claim_runtime(self.sessions, "worker:" + worker_id, self.now)
        self.resources = RuntimeResources(
            self.sessions, self.snapshot, worker_id=worker_id, clock=lambda: self.now
        )
        self.server = KubeProtocol(self.snapshot.configuration)
        self.api = KubernetesApi(
            self.snapshot,
            clients=self.server.clients,
            effect_guard=lambda: require_runtime_claim(
                self.sessions, self.claim, self.now
            ),
        )
        self.adapter = KubernetesWorkerRuntime(
            self.settings,
            fixtures.Configurations(self.settings.root),
            self.snapshot,
            self.resources,
            api=self.api,
            claim=self.claim,
        )
        self.token = "fixture-control-secret"

    def tearDown(self):
        if hasattr(self, "client"):
            fixtures.ControlPlaneTests.tearDown(self)

    def deployment(self):
        return next(
            v for (g, k, ns, n), v in self.server.objects.items() if k == "deployment"
        )

    def test_create_replay_binds_one_deployment_and_no_ambient_provider_grants(self):
        identifier = self.adapter.create(self.worker, self.document, self.token)
        self.assertEqual(
            self.adapter.create(self.worker, self.document, self.token), identifier
        )
        deployment = self.deployment()
        spec = deployment["spec"]
        self.assertEqual(spec["replicas"], 0)
        self.assertEqual(spec["strategy"], {"type": "Recreate"})
        pod = spec["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(
            pod["containers"][0]["image"],
            self.snapshot.configuration["images"]["simple"],
        )
        env = json.dumps(pod["containers"][0]["env"])
        for forbidden in (
            self.token,
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GITHUB_TOKEN",
            "GITLAB_TOKEN",
            "CONTROL_DATABASE_URL",
            "envFrom",
        ):
            self.assertNotIn(forbidden, json.dumps(pod))
        self.assertIn("FACTORY_CONTROL_TOKEN", env)
        self.assertTrue(any("persistentVolumeClaim" in v for v in pod["volumes"]))
        self.assertFalse(any("hostPath" in v for v in pod["volumes"]))
        self.assertEqual(sum(k[1] == "deployment" for k in self.server.objects), 1)

    def test_lost_create_reply_recovers_only_owned_exact_spec(self):
        self.server.lose_create = "deployment"
        with self.assertRaises(RuntimeError):
            self.adapter.create(self.worker, self.document, self.token)
        identifier = self.deployment()["metadata"]["uid"]
        self.assertEqual(
            self.adapter.create(self.worker, self.document, self.token), identifier
        )
        self.deployment()["metadata"]["uid"] = "foreign"
        before = copy.deepcopy(self.server.objects)
        with self.assertRaises(ValueError):
            self.adapter.action(self.worker.id, "start")
        self.assertEqual(self.server.objects, before)

    def test_namespace_and_legacy_mismatch_refused_before_creation(self):
        self.worker.protocol = "legacy"
        with self.assertRaises(ValueError):
            self.adapter.create(self.worker, self.document, self.token)
        self.assertEqual(self.server.objects, {})
        self.worker.protocol = "shared-v1"
        self.server.namespace_uids["flumic-workers"] = "foreign"
        with self.assertRaises(ValueError):
            self.adapter.create(self.worker, self.document, self.token)
        self.assertEqual(self.server.objects, {})

    def test_stop_remove_retain_pvc_and_only_delete_known_uids(self):
        self.adapter.create(self.worker, self.document, self.token)
        self.adapter.action(self.worker.id, "start")
        self.assertEqual(self.deployment()["spec"]["replicas"], 1)
        self.adapter.action(self.worker.id, "stop")
        self.assertEqual(self.deployment()["spec"]["replicas"], 0)
        self.adapter.action(self.worker.id, "remove")
        self.adapter.action(self.worker.id, "remove")
        self.assertFalse(any(k[1] == "deployment" for k in self.server.objects))
        pvcs = [
            v
            for k, v in self.server.objects.items()
            if k[1] == "persistent_volume_claim"
        ]
        self.assertEqual(len(pvcs), 1)
        self.assertFalse(pvcs[0]["metadata"].get("ownerReferences"))
        retained = self.adapter.list()[0]
        self.assertEqual(retained["status"], "removed")
        self.assertTrue(retained["spool_retained"])
        self.assertEqual(retained["spool_uid"], pvcs[0]["metadata"]["uid"])

    def test_configure_waits_for_old_pod_and_pending_status_is_not_healthy(self):
        from app.ports import RuntimePending

        self.adapter.create(self.worker, self.document, self.token)
        deployment = self.deployment()
        deployment["spec"]["replicas"] = 1
        pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": "old-pod",
                "uid": "old-pod-uid",
                "labels": deployment["spec"]["template"]["metadata"]["labels"],
                "ownerReferences": [
                    {
                        "kind": "ReplicaSet",
                        "uid": "rs-uid",
                        "name": "worker-rs",
                        "controller": True,
                    }
                ],
            },
            "spec": {
                "containers": deployment["spec"]["template"]["spec"]["containers"]
            },
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {
                        "name": "worker",
                        "ready": False,
                        "state": {"waiting": {"reason": "ImagePullBackOff"}},
                    }
                ],
            },
        }
        self.server.objects[("apps", "replica_set", "flumic-workers", "worker-rs")] = {
            "metadata": {
                "name": "worker-rs",
                "uid": "rs-uid",
                "ownerReferences": [
                    {
                        "kind": "Deployment",
                        "uid": deployment["metadata"]["uid"],
                        "controller": True,
                    }
                ],
            }
        }
        self.server.objects[("core", "pod", "flumic-workers", "old-pod")] = pod
        status = self.adapter.list()[0]
        self.assertEqual(status["status"], "image-pull-backoff")
        with self.assertRaises(RuntimePending):
            self.adapter.configure(self.worker, self.document, self.token)
        self.assertEqual(deployment["spec"]["replicas"], 0)

    def test_configure_reuses_uid_and_immutable_payload_versions(self):
        uid = self.adapter.create(self.worker, self.document, self.token)
        changed = dict(self.document, normal_slots=3)
        self.assertEqual(
            self.adapter.configure(self.worker, changed, "rotated-worker-token"), uid
        )
        self.assertEqual(
            self.adapter.configure(self.worker, changed, "rotated-worker-token"), uid
        )
        self.assertEqual(self.deployment()["metadata"]["uid"], uid)
        self.assertEqual(
            sum(k[1] == "persistent_volume_claim" for k in self.server.objects), 1
        )
        self.assertEqual(sum(k[1] == "secret" for k in self.server.objects), 2)
        self.adapter.action(self.worker.id, "start")
        self.assertEqual(self.deployment()["spec"]["replicas"], 1)

    def test_matching_labels_cannot_authorize_foreign_pod_logs(self):
        self.adapter.create(self.worker, self.document, self.token)
        self.server.objects[("core", "pod", "flumic-workers", "foreign")] = {
            "metadata": {
                "name": "foreign",
                "uid": "foreign-uid",
                "labels": self.deployment()["spec"]["template"]["metadata"]["labels"],
                "ownerReferences": [],
            },
            "status": {"phase": "Running"},
        }
        with self.assertRaises(ValueError):
            self.adapter.logs(self.worker.id, 100)
        self.assertFalse(any(c[0] == "logs" for c in self.server.calls))

    def test_injected_provider_environment_is_not_accepted_on_retry(self):
        self.adapter.create(self.worker, self.document, self.token)
        self.deployment()["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [
            {"secretRef": {"name": "foreign-provider"}}
        ]
        with self.assertRaises(ValueError):
            self.adapter.create(self.worker, self.document, self.token)

    def test_worker_log_bounds_and_raw_control_token_redaction(self):
        self.adapter.create(self.worker, self.document, self.token)
        deployment = self.deployment()
        self.server.objects[("apps", "replica_set", "flumic-workers", "rs")] = {
            "metadata": {
                "name": "rs",
                "uid": "rs-uid",
                "ownerReferences": [
                    {
                        "kind": "Deployment",
                        "controller": True,
                        "uid": deployment["metadata"]["uid"],
                    }
                ],
            }
        }
        self.server.objects[("core", "pod", "flumic-workers", "owned-pod")] = {
            "metadata": {
                "name": "owned-pod",
                "uid": "pod-uid",
                "labels": deployment["spec"]["template"]["metadata"]["labels"],
                "ownerReferences": [
                    {
                        "kind": "ReplicaSet",
                        "controller": True,
                        "name": "rs",
                        "uid": "rs-uid",
                    }
                ],
            },
            "status": {"phase": "Pending"},
        }
        for reason, expected in (
            ("ImagePullBackOff", "image-pull-backoff"),
            ("CrashLoopBackOff", "crash-loop-backoff"),
        ):
            self.server.objects[("core", "pod", "flumic-workers", "owned-pod")][
                "status"
            ]["containerStatuses"] = [{"state": {"waiting": {"reason": reason}}}]
            self.assertEqual(self.adapter.list()[0]["status"], expected)
        self.server.objects[("core", "pod", "flumic-workers", "owned-pod")][
            "status"
        ] = {
            "phase": "Pending",
            "conditions": [
                {"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}
            ],
        }
        self.assertEqual(self.adapter.list()[0]["status"], "unschedulable")
        self.server.log_bytes = (
            "".join("old-line-" + str(index) + "\n" for index in range(1000))
            + "diagnostic "
            + self.token
            + "\nlatest-line\n"
        ).encode()
        logs = self.adapter.logs(self.worker.id, 2)
        self.assertNotIn(self.token, logs)
        self.assertIn("[REDACTED]", logs)
        self.assertIn("latest-line", logs)
        self.assertNotIn("old-line-", logs)
        self.assertLessEqual(len(logs.encode()), 65536)
        from app.reconciler import worker_token

        real_token = worker_token(self.settings, self.worker.id)
        self.server.log_bytes = (
            "x" * (65536 - (len(real_token) - 1)) + real_token + "after-boundary"
        ).encode()
        logs = self.adapter.logs(self.worker.id, 500)
        self.assertFalse(real_token[:-1] in logs)
        self.assertLessEqual(len(logs.encode()), 65536)

    def test_configure_recovers_lost_patch_response_without_replacement(self):
        from app.adapters.kubernetes_api import KubernetesError

        uid = self.adapter.create(self.worker, self.document, self.token)
        self.server.lose_patch = "deployment"
        changed = dict(self.document, normal_slots=4)
        with self.assertRaises(KubernetesError):
            self.adapter.configure(self.worker, changed, self.token)
        generation = self.deployment()["metadata"]["generation"]
        self.assertEqual(self.adapter.configure(self.worker, changed, self.token), uid)
        self.assertEqual(self.deployment()["metadata"]["generation"], generation)

    def test_same_uid_with_modified_worker_image_cannot_start(self):
        self.adapter.create(self.worker, self.document, self.token)
        self.deployment()["spec"]["template"]["spec"]["containers"][0]["image"] = (
            "foreign.example/image@sha256:" + "b" * 64
        )
        with self.assertRaises(ValueError):
            self.adapter.action(self.worker.id, "start")
        self.assertEqual(self.deployment()["spec"]["replicas"], 0)

    def test_retained_volume_with_foreign_garbage_collection_owner_is_refused(self):
        self.adapter.create(self.worker, self.document, self.token)
        pvc = next(
            value
            for key, value in self.server.objects.items()
            if key[1] == "persistent_volume_claim"
        )
        pvc["metadata"]["ownerReferences"] = [
            {
                "kind": "Deployment",
                "uid": self.deployment()["metadata"]["uid"],
                "name": self.deployment()["metadata"]["name"],
            }
        ]
        with self.assertRaises(ValueError):
            self.adapter.create(self.worker, self.document, self.token)

    def test_unbound_same_name_foreign_deployment_is_never_adopted(self):
        body = self.adapter._bodies(self.worker, self.document, self.token)[0]
        body["metadata"]["annotations"]["flumic.io/worker-id"] = "foreign-worker"
        body["metadata"].update(uid="foreign-uid", resourceVersion="1", generation=1)
        self.server.objects[
            ("apps", "deployment", "flumic-workers", body["metadata"]["name"])
        ] = body
        with self.assertRaises(ValueError):
            self.adapter.create(self.worker, self.document, self.token)
        self.assertEqual(len(self.server.objects), 1)

    def test_start_refuses_replaced_payload_uid(self):
        self.adapter.create(self.worker, self.document, self.token)
        secret = next(
            value for key, value in self.server.objects.items() if key[1] == "secret"
        )
        secret["metadata"]["uid"] = "replacement-secret"
        with self.assertRaises(ValueError):
            self.adapter.action(self.worker.id, "start")
        self.assertEqual(self.deployment()["spec"]["replicas"], 0)

    def test_remove_refuses_spool_with_new_gc_owner(self):
        self.adapter.create(self.worker, self.document, self.token)
        pvc = next(
            value
            for key, value in self.server.objects.items()
            if key[1] == "persistent_volume_claim"
        )
        pvc["metadata"]["ownerReferences"] = [
            {"kind": "Deployment", "uid": self.deployment()["metadata"]["uid"]}
        ]
        with self.assertRaises(ValueError):
            self.adapter.action(self.worker.id, "remove")
        self.assertTrue(any(key[1] == "deployment" for key in self.server.objects))

    def test_server_canonical_storage_quantity_and_defaults_preserve_worker_intent(
        self,
    ):
        self.server.server_defaults = True
        uid = self.adapter.create(self.worker, self.document, self.token)
        self.assertEqual(
            self.adapter.create(self.worker, self.document, self.token), uid
        )
        self.adapter.action(self.worker.id, "start")
        self.adapter.action(self.worker.id, "stop")
        self.adapter.action(self.worker.id, "remove")
        self.assertTrue(self.adapter.list()[0]["spool_retained"])

    def test_ready_pod_is_running_while_registration_remains_separate(self):
        self.adapter.create(self.worker, self.document, self.token)
        self.adapter.action(self.worker.id, "start")
        deployment = self.deployment()
        self.server.objects[("apps", "replica_set", "flumic-workers", "ready-rs")] = {
            "metadata": {
                "name": "ready-rs",
                "uid": "ready-rs-uid",
                "ownerReferences": [
                    {
                        "kind": "Deployment",
                        "uid": deployment["metadata"]["uid"],
                        "controller": True,
                    }
                ],
            }
        }
        self.server.objects[("core", "pod", "flumic-workers", "ready-pod")] = {
            "metadata": {
                "name": "ready-pod",
                "uid": "ready-pod-uid",
                "labels": deployment["spec"]["template"]["metadata"]["labels"],
                "ownerReferences": [
                    {
                        "kind": "ReplicaSet",
                        "name": "ready-rs",
                        "uid": "ready-rs-uid",
                        "controller": True,
                    }
                ],
            },
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {
                        "name": "worker",
                        "ready": True,
                        "state": {"running": {"startedAt": "fixture"}},
                    }
                ],
            },
        }
        self.assertEqual(self.adapter.list()[0]["status"], "running")
        self.assertIsNone(self.worker.registered_at)


class KubernetesLifecycleTests(unittest.TestCase):
    setUp = fixtures.ControlPlaneTests.setUp
    tearDown = fixtures.ControlPlaneTests.tearDown

    def shared_worker(self):
        from app.database import Worker, Operation
        from sqlalchemy import select

        pool = self.client.post(
            "/api/v1/worker-pools", json={"name": "lifecycle-pool", "role": "simple"}
        ).json()["id"]
        identity = self.client.post(
            "/api/v1/worker-pools/" + pool + "/workers",
            json={"name": "lifecycle-worker"},
        ).json()["id"]
        with self.sessions() as db:
            return db.get(Worker, identity), db.scalar(
                select(Operation).where(Operation.worker_id == identity)
            ).id

    def test_pending_observation_retries_instead_of_failing_operation(self):
        from app.ports import RuntimePending
        from app.reconciler import reconcile_once
        from app.database import Operation, Worker

        worker, operation = self.shared_worker()
        self.runtime.create.side_effect = RuntimePending("Waiting for observation")
        self.assertTrue(reconcile_once(self.sessions, self.runtime, self.settings))
        with self.sessions() as db:
            self.assertEqual(db.get(Operation, operation).status, "pending")
            self.assertEqual(
                db.get(Worker, worker.id).desired_state, worker.desired_state
            )
        self.runtime.create.side_effect = None
        self.assertTrue(reconcile_once(self.sessions, self.runtime, self.settings))
        with self.sessions() as db:
            self.assertEqual(db.get(Operation, operation).status, "completed")

    def test_active_worker_claim_prevents_another_controller_effect(self):
        import time
        from app.runtime_claims import claim_runtime
        from app.reconciler import reconcile_once
        from app.database import Operation

        worker, operation = self.shared_worker()
        claim_runtime(self.sessions, "worker:" + worker.id, time.time())
        reconcile_once(self.sessions, self.runtime, self.settings)
        self.runtime.create.assert_not_called()
        with self.sessions() as db:
            self.assertEqual(db.get(Operation, operation).status, "pending")

    def test_stale_lifecycle_owner_cannot_commit_completion(self):
        from app.reconciler import reconcile_once
        from app.database import Operation, RuntimeLease, Worker

        worker, operation = self.shared_worker()

        def invalidate(*args):
            with self.sessions.begin() as db:
                lease = db.get(RuntimeLease, "worker:" + worker.id)
                if lease is not None:
                    lease.epoch += 1

        self.runtime.create.side_effect = invalidate
        reconcile_once(self.sessions, self.runtime, self.settings)
        with self.sessions() as db:
            self.assertNotEqual(db.get(Operation, operation).status, "completed")
            self.assertEqual(
                db.get(Worker, worker.id).desired_state, worker.desired_state
            )

    # Additional lifecycle ordering and recovery tests live here deliberately:
    # these exercise the controller, independent of Kubernetes wire fixtures.
    def test_pending_worker_does_not_starve_other_workers(self):
        from app.ports import RuntimePending
        from app.reconciler import reconcile_once
        from app.database import Worker, Operation
        from sqlalchemy import select

        first, first_op = self.shared_worker()
        with self.sessions() as db:
            pool = db.get(Worker, first.id).pool_id
        second = self.client.post(
            "/api/v1/worker-pools/" + pool + "/workers", json={"name": "second-worker"}
        ).json()["id"]
        self.runtime.create.side_effect = lambda worker, *args: (
            (_ for _ in ()).throw(RuntimePending())
            if worker.id == first.id
            else "created"
        )
        reconcile_once(self.sessions, self.runtime, self.settings)
        reconcile_once(self.sessions, self.runtime, self.settings)
        with self.sessions() as db:
            operation = db.scalar(
                select(Operation).where(Operation.worker_id == second)
            )
            self.assertEqual(operation.status, "completed")
            self.assertEqual(db.get(Operation, first_op).status, "pending")

    def test_connection_loss_keeps_operation_recoverable(self):
        from app.adapters.kubernetes_api import KubernetesError
        from app.reconciler import reconcile_once
        from app.database import Operation

        worker, operation = self.shared_worker()
        self.runtime.create.side_effect = KubernetesError(503)
        reconcile_once(self.sessions, self.runtime, self.settings)
        with self.sessions() as db:
            self.assertEqual(db.get(Operation, operation).status, "pending")
        self.runtime.create.side_effect = None
        reconcile_once(self.sessions, self.runtime, self.settings)
        with self.sessions() as db:
            self.assertEqual(db.get(Operation, operation).status, "completed")

    def test_stale_claim_heartbeat_cannot_extend_operation_lease(self):
        import time
        from app.reconciler import operation_heartbeat
        from app.runtime_claims import claim_runtime
        from app.database import Operation, RuntimeLease

        worker, operation = self.shared_worker()
        claim = claim_runtime(self.sessions, "worker:" + worker.id, time.time())
        with self.sessions.begin() as db:
            row = db.get(Operation, operation)
            row.status, row.updated_at = "running", 123
            db.get(RuntimeLease, "worker:" + worker.id).epoch += 1
        with operation_heartbeat(self.sessions, operation, interval=0.01, claim=claim):
            time.sleep(0.04)
        with self.sessions() as db:
            self.assertEqual(db.get(Operation, operation).updated_at, 123)
