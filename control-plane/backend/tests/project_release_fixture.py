"""Deterministic release fixture; no GitHub/provider calls or user worker changes.

Real HTTP routes, assignment authentication, pinning, builtin observer/spool,
state, broker capture and accounting are used. Only the GitHub/model action and
container driver are fake. Opt-in integration can replace the driver/transport.
"""

import copy
import os
import hashlib
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import test_api as fixtures
from app import database as models
from app.projects import ProjectScope
from app.reconciler import worker_token
from app.job_broker import JobBroker
from app.workflow_runs import DynamicRuntime
from app.job_contracts import JobCapture, RuntimeJobRef, RuntimeJobState
from app.project_context import decrypt
from factory.shared_worker import SharedControl, SharedSettings, SharedRequestError
from factory.adapters.shared_history import SharedPublisher
from factory.adapters.pricing import Pricing
from factory.workflows.contracts import ActionResult, RunInvocation
from factory.application.telemetry import ModelUsage, model_call, record_conversation


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def fake_builtin(action, context, inputs, transport, *, observer):
    """Use the fixed issue.load contract with fixture-only external integrations."""
    assert action == "issue.load@1"
    owner = context.settings.repository.split("/")[0]
    version = context.settings.openai_model.rsplit("-", 1)[-1]
    assert dict(context.credentials) == {"github": owner + "-github-" + version}
    with observer.observe("load_issue", {"repository": context.settings.repository}):
        with model_call("fixture", context.settings.openai_model) as call:
            call.record(
                ModelUsage(input_tokens=1, output_tokens=2, reported_cost_usd=0.01)
            )
            record_conversation(
                request=context.credentials["github"],
                response=owner + " fixture exchange",
            )
        transport.put_state(
            context,
            "release-fixture",
            {"owner": owner, "model": context.settings.openai_model},
        )
    return ActionResult(
        output={
            "issue_number": inputs.data["command"]["issue_number"],
            "state": "open",
            "labels": [owner],
        }
    )


class FakeReleaseJobs:
    def __init__(self):
        self.jobs = {}
        self.launches = {}
        self.ensures = []
        self.creations = []
        self.cleaned = []
        self.terminated = []
        self.crash_after_create = False
        self.crash_before_capture = False
        self.wait = False

    def ensure(self, launch):
        self.ensures.append(launch.job_id)
        if launch.job_id not in self.jobs:
            self.creations.append(launch.job_id)
            self.jobs[launch.job_id] = "running" if self.wait else "exited"
            self.launches[launch.job_id] = launch
        if self.crash_after_create:
            self.crash_after_create = False
            raise RuntimeError("Fixture broker crash after create")
        return RuntimeJobRef(
            **{key: getattr(launch, key) for key in RuntimeJobRef.model_fields}
        )

    def inspect(self, ref):
        return RuntimeJobState(state=self.jobs.get(ref.job_id, "missing"), exit_code=0)

    def collect(self, ref):
        if self.crash_before_capture:
            self.crash_before_capture = False
            raise RuntimeError("Fixture broker crash before capture")
        launch = self.launches[ref.job_id]
        credentials = dict(launch.credentials)
        assert set(credentials) == {"openai"}
        assert digest(credentials["openai"]) == launch.inputs["grant_hash"]
        assert launch.context["project_id"] == launch.inputs["project_id"]
        assert launch.context["repository_id"] == launch.inputs["repository_id"]
        assert launch.inputs["left"] == launch.inputs["right"]
        owner = launch.inputs["left"][0]
        return JobCapture(
            exit_code=0,
            output={"owner": owner, "repository_id": launch.inputs["repository_id"]},
            stdout=credentials["openai"],
            usage=[
                {
                    "id": "job-call",
                    "provider": "fixture",
                    "model": "fixture-" + owner,
                    "input_tokens": 3,
                    "output_tokens": 4,
                    "reported_cost_usd": 0.02,
                }
            ],
        )

    def terminate(self, ref, grace_seconds=5):
        self.terminated.append(ref.job_id)
        if ref.job_id in self.jobs:
            self.jobs[ref.job_id] = "exited"

    def cleanup(self, ref):
        self.cleaned.append(ref.job_id)
        self.jobs.pop(ref.job_id, None)


class ProjectReleaseFixture:
    def __init__(
        self,
        *,
        image="example/fixture@sha256:" + "a" * 64,
        command=None,
        directory=None,
        settings_transform=None,
        worker_target_id="local-docker",
    ):
        self.case = fixtures.ControlPlaneTests()
        self.case.setUp()
        if settings_transform is not None:
            from app.main import create_app
            from fastapi.testclient import TestClient

            self.case.client.__exit__(None, None, None)
            self.case.settings = settings_transform(self.case.settings)
            self.case.app = create_app(
                self.case.settings,
                self.case.sessions,
                self.case.runtime,
                self.case.workflow,
            )
            self.case.client = TestClient(self.case.app)
            self.case.client.__enter__()
            csrf = self.case.client.post(
                "/api/v1/auth/login", json={"password": "test-admin-password"}
            ).json()["csrf"]
            self.case.client.headers.update(
                {"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid.uuid4())}
            )
        self.client, self.sessions, self.settings = (
            self.case.client,
            self.case.sessions,
            self.case.settings,
        )
        self.directory = tempfile.TemporaryDirectory(
            prefix="project-release-", dir=directory
        )
        self.namespace = "flumic-release-" + uuid.uuid4().hex[:10]
        self.command = command or ["python", "-c", "pass"]
        self.root = Path(self.directory.name)
        self.offset = 0
        self.clock = lambda: time.time() + self.offset
        self.config = fixtures.Configurations(self.settings.root)
        self.runtime = self.case.app.state.dynamic_runtime
        self.runtime.clock = self.clock
        self.runtime.store.clock = self.clock
        self.runtime.broker = JobBroker(
            self.sessions, self.settings, clock=self.clock, root=self.root / "jobs"
        )
        self.store = self.runtime.store
        self.jobs = FakeReleaseJobs()
        self.image = image
        self.projects = {}
        self.pools = {}
        self.workers = {}
        self.controls = {}
        self.headers = {}
        self.claims = []
        # The real-engine contract establishes completed-child reattachment.
        self.child_receipts = {}
        try:
            for role in ("simple", "developer"):
                actions = (
                    ["core.echo@1", "issue.load@1"]
                    if role == "simple"
                    else ["core.echo@1", "docker.run@1"]
                )
                pool = self.call(
                    "/api/v1/worker-pools",
                    {
                        "name": "release-" + role,
                        "role": role,
                        "actions": actions,
                        "executors": ["builtin@1", "docker@1"],
                        "hatchet_tls_strategy": os.environ.get(
                            "HATCHET_CLIENT_TLS_STRATEGY", "tls"
                        ),
                    },
                    201,
                )
                self.pools[role] = pool
                worker = self.call(
                    "/api/v1/worker-pools/" + pool["id"] + "/workers",
                    {
                        "name": self.namespace + "-" + role,
                        "target_id": worker_target_id,
                    },
                    201,
                )
                self.workers[role] = worker
                self.headers[role] = {
                    "X-Worker-Id": worker["id"],
                    "Authorization": "Bearer "
                    + worker_token(self.settings, worker["id"]),
                }
                self.controls[role] = self.control(role)
            for slug in ("release-alpha", "release-beta"):
                self.project(slug)
        except Exception:
            self.close()
            raise

    def call(self, path, body=None, status=200, *, method="POST", headers=None):
        response = (
            self.client.get(path, headers=headers)
            if body is None
            else self.client.request(method, path, json=body, headers=headers)
        )
        if response.status_code != status:
            raise AssertionError(f"{path}: {response.status_code} {response.text}")
        return response.json()

    def control(self, role):
        pool, worker = self.pools[role], self.workers[role]
        settings = SharedSettings(
            pool_id=pool["id"],
            worker_id=worker["id"],
            worker_name=worker["name"],
            role=role,
            actions=pool["actions"],
            executors=pool["executors"],
        )
        control = SharedControl(
            settings,
            {
                "FACTORY_CONTROL_ENDPOINT": "http://testserver",
                "FACTORY_CONTROL_TOKEN": worker_token(self.settings, worker["id"]),
            },
        )

        def request(path, body=None):
            response = self.client.post(path, json=body, headers=self.headers[role])
            if response.status_code not in (200, 202):
                raise SharedRequestError(response.status_code)
            result = response.json()
            if path == "/api/v1/shared-worker/claim":
                self.claims.append(
                    (role, result.get("assignment_id"), result.get("action"))
                )
            return result

        control.request = request
        control.history = SharedPublisher(
            control, self.root / ("history-" + role), start=False
        )
        try:
            control.register()
        except Exception:
            control.shutdown()
            raise
        return control

    def project(self, slug):
        project = self.call("/api/v1/projects", {"slug": slug, "name": slug}, 201)
        base = "/api/v1/projects/" + project["id"]
        for pool in self.pools.values():
            self.call(
                base + "/worker-pools/" + pool["id"], {"enabled": True}, method="PUT"
            )
        secrets = {
            purpose: self.call(
                base + "/secrets",
                {
                    "name": purpose + "-fixture",
                    "purpose": purpose,
                    "value": slug + "-" + purpose + "-v1",
                },
                201,
            )
            for purpose in ("github", "openai")
        }
        repositories = {
            alias: self.call(
                base + "/repositories",
                {
                    "alias": alias,
                    "owner": slug,
                    "name": alias,
                    "credential_id": secrets["github"]["id"],
                    "overrides": {"openai_model": "fixture-" + slug + "-v1"},
                },
                201,
            )
            for alias in ("source", "target")
        }
        for repo in repositories.values():
            self.store.enable_dynamic_repository(
                ProjectScope(project["id"]), repo["id"]
            )
        graph = self.graph(secrets["openai"]["id"])
        definition = self.call(base + "/workflow-definitions", {"document": graph}, 201)
        self.projects[slug] = {
            "id": project["id"],
            "base": base,
            "secrets": secrets,
            "repositories": repositories,
            "graph": graph,
            "definition": definition,
            "version": "v1",
        }
        return self.projects[slug]

    def graph(self, openai):
        simple, developer = self.pools["simple"]["id"], self.pools["developer"]["id"]
        return {
            "schema_version": 1,
            "name": "release-mixed",
            "repositories": ["source", "target"],
            "default_repository": "source",
            "stages": [
                {
                    "id": "load",
                    "executor": "builtin",
                    "action": "issue.load@1",
                    "role": "simple",
                    "pool_id": simple,
                    "inputs": {"command": {"issue_number": 2}},
                },
                {
                    "id": "left",
                    "executor": "builtin",
                    "action": "core.echo@1",
                    "role": "simple",
                    "pool_id": simple,
                    "after": ["load"],
                    "inputs": {"labels": {"$ref": "stages.load.output.labels"}},
                },
                {
                    "id": "right",
                    "executor": "builtin",
                    "action": "core.echo@1",
                    "role": "developer",
                    "pool_id": developer,
                    "repository": "target",
                    "after": ["load"],
                    "inputs": {"labels": {"$ref": "stages.load.output.labels"}},
                },
                {
                    "id": "optional",
                    "executor": "builtin",
                    "action": "core.echo@1",
                    "role": "simple",
                    "pool_id": simple,
                    "after": ["load"],
                    "when": {"ref": "input.optional", "equals": True},
                },
                {
                    "id": "collect",
                    "executor": "docker",
                    "role": "developer",
                    "pool_id": developer,
                    "repository": "target",
                    "after": ["left", "right"],
                    "secrets": {"openai": openai},
                    "image": self.image,
                    "command": self.command,
                    "inputs": {
                        "left": {"$ref": "stages.left.output.labels"},
                        "right": {"$ref": "stages.right.output.labels"},
                        "project_id": {"$ref": "context.project_id"},
                        "repository_id": {"$ref": "context.repository_id"},
                        "grant_hash": {"$ref": "input.grant_hash"},
                        "delay_seconds": {"$ref": "input.delay_seconds"},
                    },
                },
                {
                    "id": "done",
                    "executor": "builtin",
                    "action": "core.echo@1",
                    "role": "simple",
                    "pool_id": simple,
                    "repository": "target",
                    "after": ["collect"],
                    "inputs": {"result": {"$ref": "stages.collect.output"}},
                },
            ],
        }

    def launch(self, slug, *, key=None, delay_seconds=0):
        project = self.projects[slug]
        revision = project["definition"]["revision_id"]
        headers = {"Idempotency-Key": key or slug + "-" + project["version"]}
        run = self.call(
            project["base"] + "/workflow-runs",
            {
                "revision_id": revision,
                "input": {
                    "grant_hash": digest(slug + "-openai-" + project["version"]),
                    "optional": False,
                    "delay_seconds": delay_seconds,
                },
                "bindings": {
                    project["repositories"]["source"]["id"]: {
                        kind: revision for kind in ("issue", "feedback", "verify")
                    }
                },
            },
            202,
            headers=headers,
        )
        return RunInvocation(project_id=project["id"], run_id=run["id"])

    def execute(self, invocation, role):
        if invocation.execution_id in self.child_receipts:
            return self.child_receipts[invocation.execution_id]
        with (
            patch("factory.bootstrap.run_builtin", side_effect=fake_builtin),
            patch("factory.adapters.control.Pricing", return_value=Pricing()),
        ):
            receipt = self.controls[role].execute(invocation)
            self.child_receipts[invocation.execution_id] = receipt
            return receipt

    def drain_history(self):
        for control in self.controls.values():
            control.history.flush_once()

    def finish(self, runs, *, rounds=30):
        frontiers = []
        for _ in range(rounds):
            self.offset += 1
            for run in runs:
                frontier = self.runtime.advance(run)
                frontiers.append(
                    (
                        run.project_id,
                        [
                            frontier.routes[v.execution_id].stage_id
                            for v in frontier.ready
                        ],
                    )
                )
                for invocation in frontier.ready:
                    self.execute(
                        invocation, frontier.routes[invocation.execution_id].role
                    )
            self.runtime.broker.reconcile_once(self.jobs)
            self.runtime.reconcile_background()
            self.drain_history()
            states = [self.detail(run)["state"] for run in runs]
            if all(state in {"completed", "failed", "cancelled"} for state in states):
                return states, frontiers
        raise AssertionError("Fixture runs failed to settle: " + str(states))

    def detail(self, run):
        return self.call(
            f"/api/v1/projects/{run.project_id}/workflow-runs/{run.run_id}"
        )

    def rotate(self, slug):
        project = self.projects[slug]
        base = project["base"]
        for purpose, parent in list(project["secrets"].items()):
            project["secrets"][purpose] = self.call(
                base + "/secrets",
                {
                    "name": purpose + "-fixture",
                    "purpose": purpose,
                    "value": slug + "-" + purpose + "-v2",
                    "parent_id": parent["id"],
                },
                201,
            )
        for alias, repo in list(project["repositories"].items()):
            project["repositories"][alias] = self.call(
                base + "/repositories/" + repo["id"],
                {
                    "alias": alias,
                    "owner": slug,
                    "name": alias,
                    "credential_id": project["secrets"]["github"]["id"],
                    "overrides": {"openai_model": "fixture-" + slug + "-v2"},
                    "parent_id": repo["revision_id"],
                },
                method="PUT",
            )
        graph = copy.deepcopy(project["graph"])
        next(stage for stage in graph["stages"] if stage["id"] == "collect")["secrets"][
            "openai"
        ] = project["secrets"]["openai"]["id"]
        project["definition"] = self.call(
            base + "/workflow-definitions/" + project["definition"]["id"],
            {"document": graph, "parent_id": project["definition"]["revision_id"]},
            method="PUT",
        )
        project["graph"], project["version"] = graph, "v2"

    def until_job(self, run):
        for _ in range(12):
            frontier = self.runtime.advance(run)
            for invocation in frontier.ready:
                self.execute(invocation, frontier.routes[invocation.execution_id].role)
            self.drain_history()
            with self.sessions() as db:
                job = db.scalar(
                    fixtures.select(models.RuntimeJob)
                    .join(
                        models.StageAttempt,
                        models.RuntimeJob.execution_id == models.StageAttempt.id,
                    )
                    .where(models.StageAttempt.run_id == run.run_id)
                )
                if job:
                    return job.id
        raise AssertionError("Fixture did not reach Docker acceptance")

    def restart_controller_and_broker(self):
        self.runtime = DynamicRuntime(
            self.sessions, self.settings, self.config, clock=self.clock
        )
        self.runtime.store.clock = self.clock
        self.runtime.broker = JobBroker(
            self.sessions, self.settings, clock=self.clock, root=self.root / "jobs"
        )
        self.store = self.runtime.store

    def results(self, run):
        with self.sessions() as db:
            return {
                item.stage_id: decrypt(item.result_ciphertext, self.settings)
                if item.result_ciphertext
                else None
                for item in db.scalars(
                    fixtures.select(models.StageAttempt).where(
                        models.StageAttempt.run_id == run.run_id
                    )
                )
            }

    def close(self):
        for control in self.controls.values():
            control.shutdown()
        self.directory.cleanup()
        self.case.tearDown()
