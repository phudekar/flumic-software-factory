"""Two-project native-runtime qualification; only external execution is fake."""

import copy
from dataclasses import replace
import json
from pathlib import Path
import unittest

from project_release_fixture import ProjectReleaseFixture, digest
from kubernetes_protocol_fixture import KubeProtocol
from test_runtime_targets import target
from app.runtime_targets import TargetRegistry
from app.runtime_router import RuntimeRouter
from app.adapters.kubernetes_api import KubernetesApi
from app.adapters.kubernetes_jobs import KubernetesJobRuntime
from app.job_broker import JobBroker


class NativeReleaseDriver:
    def __init__(self, fixture):
        self.fixture = fixture
        self.launches = {}
        self.creations = []
        self.releases = []
        self.snapshot = TargetRegistry(fixture.settings).snapshot("test-kubernetes")
        self.server = KubeProtocol(self.snapshot.configuration)
        self.router = RuntimeRouter(
            fixture.settings,
            fixture.config,
            fixture.sessions,
            factories={"kubernetes_jobs": self.factory},
        )
        self.router.clock = fixture.clock

    def for_job(self, ref, *, claim):
        return self.router.for_job(ref, claim=claim)

    def factory(self, snapshot, resources, claim):
        outer = self
        api = KubernetesApi(snapshot, clients=self.server.clients)
        native = KubernetesJobRuntime(
            snapshot, resources, api=api, claim=claim, clock=self.fixture.clock
        )

        def execute(name, *, expected_uid, mode):
            if mode == "release":
                outer.releases.append(expected_uid)
                return None
            pod = outer.server.objects[("core", "pod", "flumic-actions", name)]
            job_id = pod["metadata"]["annotations"]["flumic.io/job-id"]
            launch = outer.launches[job_id]
            assert set(launch.credentials) == {"openai"}
            assert digest(launch.credentials["openai"]) == launch.inputs["grant_hash"]
            assert launch.context["project_id"] == launch.inputs["project_id"]
            assert launch.context["repository_id"] == launch.inputs["repository_id"]
            assert launch.inputs["left"] == launch.inputs["right"]
            owner = launch.inputs["left"][0]
            return {
                "output": {
                    "owner": owner,
                    "repository_id": launch.inputs["repository_id"],
                },
                "usage": [
                    {
                        "id": "job-call",
                        "provider": "fixture",
                        "model": "fixture-" + owner,
                        "input_tokens": 3,
                        "output_tokens": 4,
                        "reported_cost_usd": 0.02,
                    }
                ],
            }

        api.collector_exec = execute
        original_ensure = native.ensure

        def ensure(launch):
            result = original_ensure(launch)
            if launch.job_id not in outer.launches:
                outer.launches[launch.job_id] = launch
                outer.creations.append(launch.job_id)
                root = resources.get("Job", launch.job_id)
                job = outer.server.objects[("batch", "job", root.namespace, root.name)]
                name = "pod-" + launch.job_id[:32]
                pod = {
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "metadata": {
                        "name": name,
                        "namespace": root.namespace,
                        "uid": "pod-" + launch.job_id[:24],
                        "resourceVersion": "1",
                        "labels": copy.deepcopy(
                            job["spec"]["template"]["metadata"]["labels"]
                        ),
                        "annotations": copy.deepcopy(
                            job["spec"]["template"]["metadata"]["annotations"]
                        ),
                        "ownerReferences": [
                            {
                                "apiVersion": "batch/v1",
                                "kind": "Job",
                                "name": root.name,
                                "uid": root.uid,
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
                                    "terminated": {"exitCode": 0, "reason": "Completed"}
                                },
                            },
                            {
                                "name": "collector",
                                "state": {"running": {"startedAt": "fixture"}},
                            },
                        ],
                    },
                }
                outer.server.objects[("core", "pod", root.namespace, name)] = pod
            return result

        native.ensure = ensure
        return native


class KubernetesProjectReleaseTests(unittest.TestCase):
    def fixture(self):
        def configure(settings):
            path = Path(settings.snapshot_root) / "native-targets.json"
            path.write_text(json.dumps({"schema_version": 1, "targets": [target()]}))
            return replace(settings, runtime_targets_file=path)

        f = ProjectReleaseFixture(
            settings_transform=configure, worker_target_id="test-kubernetes"
        )
        self.addCleanup(f.close)
        f.runtime.broker = JobBroker(
            f.sessions,
            f.settings,
            targets=TargetRegistry(f.settings),
            clock=f.clock,
            root=f.root / "jobs",
        )
        f.jobs = NativeReleaseDriver(f)
        return f

    def test_mixed_pool_rejects_incapable_docker_before_native_claim(self):
        import uuid
        from sqlalchemy import select
        from app.database import Assignment
        from app.reconciler import worker_token

        f = self.fixture()
        self.assertIsNone(f.settings.job_bridge)
        project = f.projects["release-alpha"]
        graph = copy.deepcopy(project["graph"])
        next(stage for stage in graph["stages"] if stage["id"] == "collect")[
            "network"
        ] = "bridge"
        project["definition"] = f.call(
            project["base"] + "/workflow-definitions/" + project["definition"]["id"],
            {"document": graph, "parent_id": project["definition"]["revision_id"]},
            method="PUT",
        )
        run = f.launch("release-alpha")
        for _ in range(12):
            frontier = f.runtime.advance(run)
            ready = [
                inv
                for inv in frontier.ready
                if frontier.routes[inv.execution_id].stage_id == "collect"
            ]
            if ready:
                invocation = ready[0]
                break
            for invocation in frontier.ready:
                f.execute(invocation, frontier.routes[invocation.execution_id].role)
            f.drain_history()
        else:
            self.fail("Container stage did not become ready")
        pool = f.pools["developer"]
        worker = f.call(
            "/api/v1/worker-pools/" + pool["id"] + "/workers",
            {"name": "incapable-docker", "target_id": "local-docker"},
            201,
        )
        headers = {
            "X-Worker-Id": worker["id"],
            "Authorization": "Bearer " + worker_token(f.settings, worker["id"]),
        }
        f.call(
            "/api/v1/shared-worker/register",
            {
                "protocol": "shared-v1",
                "pool_id": pool["id"],
                "role": "developer",
                "executors": pool["executors"],
                "actions": pool["actions"],
                "normal_slots": 2,
                "durable_slots": 16,
            },
            headers=headers,
        )
        body = {
            "project_id": run.project_id,
            "run_id": run.run_id,
            "execution_id": invocation.execution_id,
            "claim_key": str(uuid.uuid4()),
        }
        response = f.client.post(
            "/api/v1/shared-worker/claim", json=body, headers=headers
        )
        self.assertEqual(response.status_code, 409, response.text)
        with f.sessions() as db:
            row = db.scalar(
                select(Assignment).where(
                    Assignment.execution_id == invocation.execution_id
                )
            )
            self.assertEqual(row.status, "ready")
            self.assertIsNone(row.worker_id)
        response = f.client.post(
            "/api/v1/shared-worker/claim", json=body, headers=f.headers["developer"]
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_two_projects_shared_workers_native_jobs_frozen_grants_and_usage(self):
        f = self.fixture()
        runs = [f.launch(slug) for slug in f.projects]
        f.rotate("release-alpha")
        states, frontiers = f.finish(runs)
        self.assertEqual(states, ["completed", "completed"])
        self.assertEqual(len(f.jobs.creations), 2)
        self.assertEqual({role for role, _, _ in f.claims}, {"simple", "developer"})
        for run in runs:
            project = next(
                value for value in f.projects.values() if value["id"] == run.project_id
            )
            usage = f.call(project["base"] + "/usage?run_id=" + run.run_id)
            self.assertEqual(
                (usage["calls"], usage["input_tokens"], usage["output_tokens"]),
                (2, 4, 6),
            )
            self.assertAlmostEqual(usage["reported_usd"], 0.03)
            with f.sessions() as db:
                from app.database import RuntimeJob, StageAttempt
                from sqlalchemy import select

                job = db.scalar(
                    select(RuntimeJob)
                    .join(StageAttempt, RuntimeJob.execution_id == StageAttempt.id)
                    .where(StageAttempt.run_id == run.run_id)
                )
                detail = f.call(project["base"] + "/jobs/" + job.id)
                self.assertEqual(detail["target_id"], "test-kubernetes")
                self.assertEqual(detail["runtime"]["log_stream"], "combined")
                self.assertIsNotNone(detail["runtime"]["job_uid"])
                self.assertEqual(len(detail["runtime"]["pod_uids"]), 1)
                foreign = next(
                    value["base"]
                    for value in f.projects.values()
                    if value["id"] != run.project_id
                )
                self.assertEqual(
                    f.client.get(foreign + "/jobs/" + job.id).status_code, 404
                )
            self.assertNotIn("private", str(detail["runtime"]))
        self.assertTrue(
            all(
                value.target_id == "test-kubernetes"
                for value in f.jobs.launches.values()
            )
        )
