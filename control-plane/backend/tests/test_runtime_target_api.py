"""Runtime infrastructure pins are operator-owned; HTTP views are allowlisted."""

from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import unittest
import uuid
from unittest.mock import Mock

from fastapi.testclient import TestClient
import test_api as fixtures
import test_docker_jobs as job_fixtures
from test_runtime_targets import target
from app.database import Worker, Project, RuntimeJob
from app.main import create_app
from app.runtime_targets import TargetRegistry


class RuntimeTargetApiTests(unittest.TestCase):
    arrange = job_fixtures.JobBrokerTests.arrange
    submit = job_fixtures.JobBrokerTests.submit

    def setUp(self):
        fixtures.ControlPlaneTests.setUp(self)
        self.client.__exit__(None, None, None)
        path = Path(self.temporary.name) / "targets.json"
        path.write_text(json.dumps({"schema_version": 1, "targets": [target()]}))
        self.settings = replace(self.settings, runtime_targets_file=path)
        self.app = create_app(self.settings, self.sessions, self.runtime, self.workflow)
        self.client = TestClient(self.app)
        self.client.__enter__()
        csrf = self.client.post(
            "/api/v1/auth/login", json={"password": "test-admin-password"}
        ).json()["csrf"]
        self.client.headers.update(
            {"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid.uuid4())}
        )
        self.registry = TargetRegistry(self.settings)

    tearDown = fixtures.ControlPlaneTests.tearDown

    def test_qualified_native_bridge_can_publish_and_pin_without_docker_network(self):
        import test_workflow_definitions as definitions
        from app.workflow_state import WorkflowState
        from app.projects import ProjectScope

        project, base, repositories, _, graph = (
            definitions.WorkflowDefinitionTests.setup_graph(self)
        )
        graph["stages"][1]["network"] = "bridge"
        self.assertIsNone(self.settings.job_bridge)
        response = self.client.post(
            base + "/workflow-definitions", json={"document": graph}
        )
        self.assertEqual(response.status_code, 201, response.text)
        state = WorkflowState(
            self.sessions, self.settings, fixtures.Configurations(self.settings.root)
        )
        scope = ProjectScope(project)
        for repository in repositories.values():
            state.enable_dynamic_repository(scope, repository["id"])
        run = state.start_run(
            scope, response.json()["revision_id"], {}, key="native-bridge"
        )
        self.assertTrue(run.snapshot_id)

    def pool(self):
        return self.client.post(
            "/api/v1/worker-pools", json={"name": "target-pool", "role": "simple"}
        ).json()["id"]

    def test_catalog_includes_both_targets_without_auth_paths_or_credentials(self):
        response = self.client.get("/api/v1/targets")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {item["id"] for item in response.json()},
            {"local-docker", "test-kubernetes"},
        )
        encoded = response.text
        for private in (
            "kubeconfig",
            "/private/",
            "test-context",
            "target_snapshot",
            "auth",
        ):
            self.assertNotIn(private, encoded)
        kube = next(item for item in response.json() if item["id"] == "test-kubernetes")
        self.assertTrue(kube["capabilities"]["shared"])
        self.assertFalse(kube["capabilities"]["legacy"])

    def test_same_pool_accepts_pinned_docker_and_kubernetes_shared_workers(self):
        pool = self.pool()
        for name, identity in (
            ("docker-shared", "local-docker"),
            ("kube-shared", "test-kubernetes"),
        ):
            response = self.client.post(
                "/api/v1/worker-pools/" + pool + "/workers",
                json={"name": name, "target_id": identity},
            )
            self.assertEqual(response.status_code, 201, response.text)
            with self.sessions() as db:
                worker = db.get(Worker, response.json()["id"])
                self.assertEqual(worker.target_id, identity)
                self.assertEqual(
                    worker.target_snapshot,
                    self.registry.snapshot(identity).model_dump(),
                )
        view = self.client.get("/api/v1/workers").json()["managed"]
        self.assertEqual(
            {item["target_id"] for item in view}, {"local-docker", "test-kubernetes"}
        )
        self.assertNotIn("target_snapshot", str(view))
        self.assertNotIn("/private/", str(view))
        self.assertNotIn("auth", str(view))

    def test_unknown_targets_legacy_kubernetes_and_infrastructure_edits_are_rejected(
        self,
    ):
        pool = self.pool()
        url = "/api/v1/worker-pools/" + pool + "/workers"
        self.assertEqual(
            self.client.post(
                url, json={"name": "unknown", "target_id": "unregistered"}
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(
                url,
                json={
                    "name": "bad-auth",
                    "target_id": "test-kubernetes",
                    "auth": {"mode": "incluster"},
                },
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(
                "/api/v1/workers",
                json={
                    "name": "legacy-kubernetes",
                    "profile": "simple",
                    "revision_id": self.revision["id"],
                    "target_id": "test-kubernetes",
                },
            ).status_code,
            422,
        )
        worker = self.client.post(
            url, json={"name": "fixed-target", "target_id": "test-kubernetes"}
        )
        self.assertEqual(worker.status_code, 201, worker.text)
        response = self.client.post(
            "/api/v1/workers/" + worker.json()["id"] + "/actions",
            json={"action": "configure", "target_id": "local-docker"},
        )
        self.assertEqual(response.status_code, 422)
        with self.sessions() as db:
            self.assertEqual(
                db.get(Worker, worker.json()["id"]).target_id, "test-kubernetes"
            )

    def native_job(self):
        self.arrange()
        from app.database import Assignment

        with self.sessions.begin() as db:
            worker = db.get(Worker, db.get(Assignment, self.assignment).worker_id)
            worker.target_id = "test-kubernetes"
            worker.target_snapshot = self.registry.snapshot(
                worker.target_id
            ).model_dump()
        return self.submit()

    def test_job_details_are_project_scoped_and_never_expose_private_pins(self):
        receipt = self.native_job()
        path = "/api/v1/projects/" + self.scope.project_id + "/jobs/" + receipt.job_id
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["target_id"], "test-kubernetes")
        self.assertEqual(response.json()["runtime"]["namespace"], "flumic-actions")
        for private in (
            "/private/",
            "kubeconfig",
            "launch_ciphertext",
            "target_snapshot",
            "private-job-key",
        ):
            self.assertNotIn(private, response.text)
        foreign = str(uuid.uuid4())
        with self.sessions.begin() as db:
            db.add(Project(id=foreign, slug="other-target-project", name="Other"))
        self.assertEqual(
            self.client.get(
                "/api/v1/projects/" + foreign + "/jobs/" + receipt.job_id
            ).status_code,
            404,
        )

    def test_worker_target_edit_does_not_redirect_an_admitted_job(self):
        self.assertIsNotNone(
            importlib.util.find_spec("app.runtime_router"),
            "Target runtime router is not implemented",
        )
        from app.runtime_router import RuntimeRouter
        from app.database import Assignment
        from app.project_context import decrypt
        from app.job_contracts import JobLaunch, RuntimeJobRef

        receipt = self.native_job()
        with self.sessions.begin() as db:
            row = db.get(RuntimeJob, receipt.job_id)
            launch = JobLaunch.model_validate(
                decrypt(row.launch_ciphertext, self.settings)["launch"]
            )
            worker = db.get(Worker, db.get(Assignment, self.assignment).worker_id)
            worker.target_id = "local-docker"
            worker.target_snapshot = self.registry.snapshot("local-docker").model_dump()
        native = Mock()
        docker = Mock()
        router = RuntimeRouter(
            self.settings,
            fixtures.Configurations(self.settings.root),
            self.sessions,
            factories={
                "kubernetes_jobs": lambda snapshot, resources, claim: native,
                "docker_jobs": lambda snapshot, resources, claim: docker,
            },
        )
        ref = RuntimeJobRef(
            **{key: getattr(launch, key) for key in RuntimeJobRef.model_fields}
        )
        self.assertIs(router.for_job(ref, claim=None), native)

    def test_malformed_kubernetes_log_handle_never_falls_back_to_docker(self):
        self.assertIsNotNone(
            importlib.util.find_spec("app.runtime_router"),
            "Target runtime router is not implemented",
        )
        from app.runtime_router import RuntimeRouter

        docker = Mock()
        router = RuntimeRouter(
            self.settings,
            fixtures.Configurations(self.settings.root),
            self.sessions,
            factories={
                "docker_workers": lambda snapshot, resources, claim: docker,
                "kubernetes_workers": lambda snapshot, resources, claim: Mock(),
            },
        )
        for handle in (
            "kubernetes:wrong",
            "kubernetes/test-kubernetes/unknown",
            "kubernetes:test-kubernetes:arbitrary-uid",
        ):
            with self.subTest(handle=handle), self.assertRaises(ValueError):
                router.logs(handle, 200)
        docker.logs.assert_not_called()

    def test_unavailable_target_does_not_hide_healthy_docker_workers(self):
        self.assertIsNotNone(
            importlib.util.find_spec("app.runtime_router"),
            "Target runtime router is not implemented",
        )
        from app.runtime_router import RuntimeRouter

        docker = Mock()
        docker.list.return_value = [
            {
                "id": "fixture",
                "instance_id": "worker",
                "managed": True,
                "status": "running",
            }
        ]

        def unavailable(*args):
            raise RuntimeError("private-transport-body")

        router = RuntimeRouter(
            self.settings,
            fixtures.Configurations(self.settings.root),
            self.sessions,
            factories={
                "docker_workers": lambda snapshot, resources, claim: docker,
                "kubernetes_workers": unavailable,
            },
        )
        items = router.list()
        self.assertEqual(items[0]["id"], "fixture")
        self.assertTrue(router.warnings)
        self.assertNotIn("private-transport-body", str(router.warnings))

    def test_real_kubernetes_composition_requires_postgresql_before_loading_sdk_credentials(
        self,
    ):
        self.assertIsNotNone(
            importlib.util.find_spec("app.runtime_router"),
            "Target runtime router is not implemented",
        )
        from app.runtime_router import RuntimeRouter
        from app.database import database

        engine, sessions = database("sqlite://")
        try:
            with self.assertRaisesRegex(ValueError, "PostgreSQL"):
                RuntimeRouter(
                    self.settings, fixtures.Configurations(self.settings.root), sessions
                )
        finally:
            engine.dispose()

    def test_container_assignment_rejects_unqualified_target_limits_before_job_submission(
        self,
    ):
        self.arrange()
        from app.database import Assignment
        from app.worker_pools import eligibility
        from fastapi import HTTPException

        document = target()
        document["pid_limit"] = 256
        self.settings.runtime_targets_file.write_text(
            json.dumps({"schema_version": 1, "targets": [document]})
        )
        registry = TargetRegistry(self.settings)
        with self.sessions.begin() as db:
            row = db.get(Assignment, self.assignment)
            worker = db.get(Worker, row.worker_id)
            worker.target_id = "test-kubernetes"
            worker.target_snapshot = registry.snapshot(worker.target_id).model_dump()
            with self.assertRaises(HTTPException) as error:
                eligibility(db, worker, row, self.settings)
            self.assertEqual(error.exception.status_code, 409)

    def test_removed_operator_pin_never_falls_back_to_docker(self):
        from app.runtime_router import RuntimeRouter
        from app.project_context import decrypt
        from app.job_contracts import JobLaunch, RuntimeJobRef

        receipt = self.native_job()
        with self.sessions() as db:
            launch = JobLaunch.model_validate(
                decrypt(
                    db.get(RuntimeJob, receipt.job_id).launch_ciphertext, self.settings
                )["launch"]
            )
        self.settings.runtime_targets_file.write_text(
            json.dumps({"schema_version": 1, "targets": []})
        )
        docker = Mock()
        router = RuntimeRouter(
            self.settings,
            fixtures.Configurations(self.settings.root),
            self.sessions,
            factories={"docker_jobs": lambda snapshot, resources, claim: docker},
        )
        ref = RuntimeJobRef(
            **{key: getattr(launch, key) for key in RuntimeJobRef.model_fields}
        )
        with self.assertRaises(ValueError):
            router.for_job(ref, claim=None)
        docker.ensure.assert_not_called()

    def test_native_log_handle_uses_its_persisted_worker_uid_with_multiple_workers(
        self,
    ):
        from app.runtime_router import RuntimeRouter
        from app.runtime_claims import require_runtime_claim
        from app.reconciler import reconcile_once, worker_token
        from app.adapters.kubernetes_api import KubernetesApi
        from app.adapters.kubernetes_workers import KubernetesWorkerRuntime
        from kubernetes_protocol_fixture import KubeProtocol

        server = KubeProtocol(self.registry.snapshot("test-kubernetes").configuration)

        def native(snapshot, resources, claim):
            guard = (
                (lambda: require_runtime_claim(self.sessions, claim, resources.clock()))
                if claim
                else None
            )
            return KubernetesWorkerRuntime(
                self.settings,
                fixtures.Configurations(self.settings.root),
                snapshot,
                resources,
                api=KubernetesApi(snapshot, clients=server.clients, effect_guard=guard),
                claim=claim,
            )

        docker = Mock()
        docker.list.return_value = []
        router = RuntimeRouter(
            self.settings,
            fixtures.Configurations(self.settings.root),
            self.sessions,
            factories={
                "docker_workers": lambda snapshot, resources, claim: docker,
                "kubernetes_workers": native,
            },
        )
        pool = self.pool()
        workers = []
        for name in ("first-native", "second-native"):
            response = self.client.post(
                "/api/v1/worker-pools/" + pool + "/workers",
                json={"name": name, "target_id": "test-kubernetes"},
            )
            self.assertEqual(response.status_code, 201, response.text)
            workers.append(response.json()["id"])
            reconcile_once(self.sessions, router, self.settings)
        deployment = next(
            value
            for key, value in server.objects.items()
            if key[1] == "deployment"
            and value["metadata"]["annotations"]["flumic.io/worker-id"] == workers[1]
        )
        server.objects[("apps", "replica_set", "flumic-workers", "logs-rs")] = {
            "metadata": {
                "name": "logs-rs",
                "uid": "logs-rs-uid",
                "ownerReferences": [
                    {
                        "kind": "Deployment",
                        "uid": deployment["metadata"]["uid"],
                        "controller": True,
                    }
                ],
            }
        }
        server.objects[("core", "pod", "flumic-workers", "logs-pod")] = {
            "metadata": {
                "name": "logs-pod",
                "uid": "logs-pod-uid",
                "labels": deployment["spec"]["template"]["metadata"]["labels"],
                "ownerReferences": [
                    {
                        "kind": "ReplicaSet",
                        "name": "logs-rs",
                        "uid": "logs-rs-uid",
                        "controller": True,
                    }
                ],
            },
            "status": {"phase": "Pending"},
        }
        handle = next(
            item["id"] for item in router.list() if item["instance_id"] == workers[1]
        )
        token = worker_token(self.settings, workers[1])
        server.log_bytes = ("diagnostic " + token).encode()
        logs = router.logs(handle, 200)
        self.assertIn("[REDACTED]", logs)
        self.assertNotIn(token, logs)
        docker.logs.assert_not_called()

    def test_existing_managed_docker_name_log_calls_remain_supported(self):
        from app.runtime_router import RuntimeRouter

        docker = Mock()
        docker.logs.return_value = "bounded Docker log"
        router = RuntimeRouter(
            self.settings,
            fixtures.Configurations(self.settings.root),
            self.sessions,
            factories={
                "docker_workers": lambda snapshot, resources, claim: docker,
                "kubernetes_workers": lambda snapshot, resources, claim: Mock(),
            },
        )
        self.assertEqual(router.logs("kubernetes-worker", 200), "bounded Docker log")
        docker.logs.assert_called_once_with("kubernetes-worker", 200)
