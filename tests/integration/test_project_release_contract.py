"""Opt-in real PostgreSQL/HTTP/Hatchet/shared-worker/Docker qualification.

FLUMIC_LIVE_PROJECT_RELEASE=1, CONTROL_TEST_DATABASE_URL (dedicated test DB),
DOCKER_JOB_CONTRACT_IMAGE (local immutable Python image) and Hatchet SDK env are
required. Only uniquely named fixture processes, schema and jobs are touched.
"""

import json
import asyncio
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import unittest
from dataclasses import asdict, replace

ROOT = Path(__file__).resolve().parents[2]

JOB_PROGRAM = """import hashlib,json,time
v=json.load(open('/flumic/input.json')); c=json.load(open('/flumic/context.json')); s=json.load(open('/flumic/credentials.json'))
assert set(s)=={'openai'} and hashlib.sha256(s['openai'].encode()).hexdigest()==v['grant_hash']
assert c['project_id']==v['project_id'] and c['repository_id']==v['repository_id']
assert v['left']==v['right']
time.sleep(v['delay_seconds']); owner=v['left'][0]
print(s['openai'])
json.dump({'output':{'owner':owner,'repository_id':v['repository_id']},'usage':[{'id':'job-call','provider':'fixture','model':'fixture-'+owner,'input_tokens':3,'output_tokens':4,'reported_cost_usd':0.02}]},open('/flumic/output/result.json','w'))
"""


def cleanup_all(operations):
    """Attempt every owned resource; retain the first failure for unittest."""
    failures = []
    for operation in operations:
        try:
            operation()
        except Exception as error:
            failures.append(error)
    if failures:
        raise failures[0]


@unittest.skipUnless(
    os.environ.get("FLUMIC_LIVE_PROJECT_RELEASE") == "1",
    "opt-in combined project release contract",
)
class LiveProjectReleaseContract(unittest.TestCase):
    def setUp(self):
        self.assertTrue(
            os.environ.get("CONTROL_TEST_DATABASE_URL", "").startswith("postgresql"),
            "Use a dedicated PostgreSQL test database",
        )
        sys.path[:0] = [
            str(ROOT / "worker"),
            str(ROOT / "control-plane/backend"),
            str(ROOT / "control-plane/backend/tests"),
        ]
        from project_release_fixture import ProjectReleaseFixture
        import uvicorn

        (ROOT / "output").mkdir(exist_ok=True)
        self.fixture = ProjectReleaseFixture(
            image=os.environ["DOCKER_JOB_CONTRACT_IMAGE"],
            command=["python", "-c", JOB_PROGRAM],
            directory=ROOT / "output",
        )
        self.addCleanup(self.fixture.close)
        self.processes = []
        self.logs = []
        self.created = {}
        self.engine_roots_settled = False
        self.ensure_counts = {}
        self.addCleanup(self.stop_live_resources)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.endpoint = "http://127.0.0.1:" + str(port)
        self.server = uvicorn.Server(
            uvicorn.Config(
                self.fixture.case.app,
                host="127.0.0.1",
                port=port,
                log_level="warning",
                access_log=False,
            )
        )
        self.server_thread = threading.Thread(target=self.server.run, daemon=True)
        self.server_thread.start()
        deadline = time.monotonic() + 20
        while not self.server.started:
            if time.monotonic() > deadline:
                raise AssertionError("Fixture API did not start")
            time.sleep(0.1)
        self.write_configs()
        self.control = self.process("control")
        self.simple = self.process("simple")
        self.developer = self.process("developer")
        self.allow_developer_exit = False
        time.sleep(5)
        from app.adapters.docker_jobs import DockerJobRuntime

        parent = self

        class ObservedDockerJobs(DockerJobRuntime):
            def ensure(self, launch):
                ref = super().ensure(launch)
                identity = self.client.containers.get("flumic-job-" + launch.job_id).id
                parent.created.setdefault(launch.job_id, set()).add(identity)
                parent.ensure_counts[launch.job_id] = (
                    parent.ensure_counts.get(launch.job_id, 0) + 1
                )
                return ref

        self.jobs = ObservedDockerJobs(
            self.fixture.root / "jobs",
            self.fixture.root / "jobs",
            self.fixture.namespace,
        )
        self.failures = []
        self.stopped = threading.Event()
        self.broker_thread = threading.Thread(target=self.broker_pump, daemon=True)
        self.broker_thread.start()

    def stop_live_resources(self):
        cleanup = []
        if hasattr(self, "broker_thread"):
            cleanup.append(self.stop_broker)
        if not self.engine_roots_settled:
            cleanup.append(self.cancel_owned_engine_roots)
        cleanup.append(self.stop_processes)
        if hasattr(self, "jobs"):
            cleanup.append(self.cleanup_jobs)
        if hasattr(self, "server"):
            cleanup.append(self.stop_server)
        cleanup_all(cleanup)

    def private_json(self, name, value):
        path = self.fixture.root / (name + ".json")
        path.write_text(json.dumps(value, default=str))
        path.chmod(0o600)
        return path

    def write_configs(self):
        f = self.fixture
        settings = replace(f.settings, runtime_namespace=f.namespace)
        self.configs = {
            "control": self.private_json(
                "control",
                {
                    "settings": asdict(settings),
                    "database_url": f.case.test_engine.url.render_as_string(
                        hide_password=False
                    ),
                    "job_root": str(f.root / "jobs"),
                },
            )
        }
        for role, control in f.controls.items():
            self.configs[role] = self.private_json(
                role,
                {
                    "settings": control.settings.model_dump(),
                    "endpoint": self.endpoint,
                    "history_root": str(f.root / ("live-history-" + role)),
                },
            )

    def process(self, role):
        from app.reconciler import worker_token

        env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "GITHUB_TOKEN",
                "OPENAI_API_KEY",
                "DEVELOPER_OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
                "CONTROL_DATABASE_URL",
                "CONTROL_TEST_DATABASE_URL",
                "CONTROL_MIGRATION_TEST_URL",
                "CONTROL_ENCRYPTION_KEY",
                "CONTROL_ADMIN_PASSWORD",
                "FACTORY_CONTROL_TOKEN",
            }
        }
        if role != "control":
            env["FACTORY_CONTROL_TOKEN"] = worker_token(
                self.fixture.settings, self.fixture.workers[role]["id"]
            )
        log = (
            self.fixture.root / (role + "-" + str(len(self.processes)) + ".log")
        ).open("w")
        self.logs.append(log)
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("project_release_worker.py")),
                role,
                str(self.configs[role]),
            ],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        self.processes.append(process)
        return process

    def broker_pump(self):
        while not self.stopped.is_set():
            try:
                self.fixture.runtime.broker.reconcile_once(self.jobs)
            except Exception as error:
                self.failures.append(type(error).__name__)
            self.stopped.wait(0.2)

    def stop_broker(self):
        self.stopped.set()
        self.broker_thread.join(timeout=10)

    def stop_server(self):
        self.server.should_exit = True
        self.server_thread.join(timeout=10)

    def cancel_owned_engine_roots(self):
        # The fixture's private PostgreSQL schema contains only its own dispatches.
        # Cancellation is cleanup intent; processes/jobs are still stopped below.
        from app.database import DynamicDispatch
        from sqlalchemy import select
        from hatchet_sdk import Hatchet

        with self.fixture.sessions() as db:
            identities = list(
                db.scalars(
                    select(DynamicDispatch.engine_id).where(
                        DynamicDispatch.kind == "run",
                        DynamicDispatch.engine_id.is_not(None),
                    )
                )
            )

        async def cancel():
            client = Hatchet()
            for identity in identities:
                status = await client.runs.aio_get_status(identity)
                if str(status.value) not in {"COMPLETED", "FAILED", "CANCELLED"}:
                    await client.runs.aio_cancel(identity)

        try:
            asyncio.run(asyncio.wait_for(cancel(), timeout=15))
        except Exception:
            # A tenant outage must not bypass local process/container cleanup.
            pass

    def stop_processes(self):
        def terminate(process):
            if process.poll() is None:
                process.terminate()

        def wait(process):
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)

        cleanup_all(
            [
                *[
                    lambda process=process: terminate(process)
                    for process in self.processes
                ],
                *[lambda process=process: wait(process) for process in self.processes],
                *[log.close for log in self.logs],
                self.save_diagnostics,
            ]
        )

    def save_diagnostics(self):
        if not self.engine_roots_settled and os.environ.get("FLUMIC_LIVE_EVIDENCE_DIR"):
            destination = Path(os.environ["FLUMIC_LIVE_EVIDENCE_DIR"]) / (
                "diagnostics-" + self.fixture.namespace
            )
            destination.mkdir(parents=True, exist_ok=True)
            destination.chmod(0o700)
            from app.reconciler import worker_token

            secrets = [
                self.fixture.settings.encryption_key,
                *[
                    value
                    for key, value in os.environ.items()
                    if any(word in key.upper() for word in ("TOKEN", "PASSWORD", "KEY"))
                    and value
                ],
                *[
                    worker_token(self.fixture.settings, worker["id"])
                    for worker in self.fixture.workers.values()
                ],
            ]
            for source in self.fixture.root.glob("*.log"):
                text = source.read_text(errors="replace")
                for secret in sorted(set(secrets), key=len, reverse=True):
                    text = text.replace(secret, "[REDACTED]")
                target = destination / source.name
                target.write_text(text[-262144:])
                target.chmod(0o600)

    def cleanup_jobs(self):
        from app.database import RuntimeJob
        from app.job_contracts import RuntimeJobRef
        from app.project_context import decrypt
        from sqlalchemy import select

        def clean(row):
            value = decrypt(row.launch_ciphertext, self.fixture.settings)["launch"]
            ref = RuntimeJobRef(
                **{key: value[key] for key in RuntimeJobRef.model_fields}
            )
            cleanup_all(
                [
                    lambda: self.jobs.terminate(ref, grace_seconds=1),
                    lambda: self.jobs.cleanup(ref),
                ]
            )

        with self.fixture.sessions() as db:
            cleanup_all(
                [lambda row=row: clean(row) for row in db.scalars(select(RuntimeJob))]
            )

    def wait(self, predicate, *, timeout=240):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            value = predicate()
            if value:
                return value
            for process in (
                self.control,
                self.simple,
                *([] if self.allow_developer_exit else [self.developer]),
            ):
                if process.poll() is not None:
                    self.fail(
                        "Fixture "
                        + str(process.pid)
                        + " exited; inspect private evidence logs"
                    )
            time.sleep(0.25)
        self.fail("Combined release contract timed out; inspect fixture evidence")

    def engine_states(self, runs):
        from hatchet_sdk import Hatchet

        identities = [self.fixture.detail(run)["dispatch"]["engine_id"] for run in runs]

        async def collect():
            client = Hatchet()
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                states = {
                    identity: str((await client.runs.aio_get_status(identity)).value)
                    for identity in identities
                }
                if all(
                    value in {"COMPLETED", "FAILED", "CANCELLED"}
                    for value in states.values()
                ):
                    self.engine_roots_settled = True
                    return states
                await asyncio.sleep(0.5)
            raise AssertionError("Fixture engine roots did not become terminal")

        return asyncio.run(collect())

    def evidence(self, mode, runs, engine_states):
        destination = os.environ.get("FLUMIC_LIVE_EVIDENCE_DIR")
        if destination:
            path = Path(destination)
            path.mkdir(parents=True, exist_ok=True)
            summary = {
                "namespace": self.fixture.namespace,
                "mode": mode,
                "projects": 2,
                "repositories_per_project": 2,
                "shared_role_workers": 2,
                "runs": [self.fixture.detail(run) for run in runs],
                "engine_states": engine_states,
                "job_container_identities": {
                    job: sorted(ids) for job, ids in self.created.items()
                },
                "provider_calls": 0,
                "github_writes": 0,
            }
            (path / ("project-release-" + mode + ".json")).write_text(
                json.dumps(summary, indent=2)
            )

    def test_two_projects_survive_control_worker_and_broker_restart_with_real_jobs(
        self,
    ):
        from app.database import RuntimeJob
        from app.job_broker import JobBroker
        from sqlalchemy import select

        f = self.fixture
        runs = [
            f.launch("release-alpha", delay_seconds=1),
            f.launch("release-beta", delay_seconds=12),
        ]
        initial = [f.detail(run) for run in runs]
        original = [detail["revision_id"] for detail in initial]
        f.rotate("release-alpha")

        def first_completed():
            with f.sessions() as db:
                return db.scalar(
                    select(RuntimeJob.id).where(RuntimeJob.state == "completed")
                )

        first = self.wait(first_completed)
        self.control.kill()
        self.control.wait(timeout=10)
        self.control = self.process("control")
        # The broker must finish accepted work even with the submitting role gone.
        self.wait(lambda: len(self.created) == 2)
        self.allow_developer_exit = True
        self.developer.kill()
        self.developer.wait(timeout=10)
        f.runtime.broker = JobBroker(f.sessions, f.settings, root=f.root / "jobs")
        self.wait(lambda: all(f.detail(run)["state"] == "completed" for run in runs))
        # Wait for action-local history threads to deliver their terminal spool.
        self.wait(
            lambda: all(
                f.call(project["base"] + "/usage?run_id=" + run.run_id)["calls"] == 2
                for project, run in zip(f.projects.values(), runs)
            ),
            timeout=30,
        )
        for index, (slug, project) in enumerate(f.projects.items()):
            detail = f.detail(runs[index])
            self.assertEqual(detail["revision_id"], original[index])
            self.assertEqual(detail["repositories"], initial[index]["repositories"])
            self.assertTrue(
                all(
                    stage["settings"]["openai_model"] == "fixture-" + slug + "-v1"
                    for stage in detail["stages"]
                )
            )
            self.assertEqual(
                {a["worker_id"] for a in detail["attempts"] if a["worker_id"]},
                set(w["id"] for w in f.workers.values()),
            )
            self.assertEqual(
                next(a for a in detail["attempts"] if a["stage_id"] == "optional")[
                    "state"
                ],
                "skipped",
            )
            usage = f.call(project["base"] + "/usage?run_id=" + runs[index].run_id)
            self.assertAlmostEqual(usage["reported_usd"], 0.03)
            self.assertEqual((usage["input_tokens"], usage["output_tokens"]), (4, 6))
            self.assertNotIn(
                "release-beta" if index == 0 else "release-alpha",
                str(f.results(runs[index])),
            )
        self.assertFalse(self.failures)
        self.assertEqual(len(self.created), 2)
        self.assertTrue(
            all(len(identities) == 1 for identities in self.created.values())
        )
        count = self.ensure_counts[first]
        time.sleep(2)
        self.assertEqual(self.ensure_counts[first], count)
        for index, (slug, project) in enumerate(f.projects.items()):
            events = f.call(
                project["base"]
                + "/events?kind=model.completed&run_id="
                + runs[index].run_id
            )
            self.assertEqual(len(events), 2)
            other = "release-beta" if slug == "release-alpha" else "release-alpha"
            self.assertNotIn(other, str(events))
            self.assertNotIn(slug + "-github-v1", str(events))
            self.assertNotIn(slug + "-openai-v1", str(events))
            event = next(
                event
                for event in events
                if event["payload"].get("conversation_available")
            )
            exchange = f.call(
                project["base"] + "/model-calls/" + event["id"] + "/conversation"
            )
            self.assertEqual(exchange["request"], "[REDACTED]")
            self.assertEqual(
                f.client.get(
                    f.projects[other]["base"]
                    + "/model-calls/"
                    + event["id"]
                    + "/conversation"
                ).status_code,
                404,
            )
        self.evidence("restart", runs, self.engine_states(runs))

    def terminal_after_worker_loss(self, mode):
        from copy import deepcopy
        from app.database import RuntimeJob
        from sqlalchemy import select

        f = self.fixture
        project = f.projects["release-alpha"]
        graph = deepcopy(project["graph"])
        next(stage for stage in graph["stages"] if stage["id"] == "collect")[
            "timeout_seconds"
        ] = 3 if mode == "deadline" else 60
        project["definition"] = f.call(
            project["base"] + "/workflow-definitions/" + project["definition"]["id"],
            {"parent_id": project["definition"]["revision_id"], "document": graph},
            method="PUT",
        )
        run = f.launch("release-alpha", key="real-" + mode, delay_seconds=30)
        self.wait(lambda: len(self.created) == 1)
        self.allow_developer_exit = True
        self.developer.kill()
        self.developer.wait(timeout=10)
        if mode == "cancel":
            receipt = f.call(
                project["base"] + "/workflow-runs/" + run.run_id + "/cancel", {}, 202
            )
            self.assertEqual(receipt["state"], "cancelling")
        self.wait(lambda: f.detail(run)["state"] in {"failed", "cancelled"})
        expected = "cancelled" if mode == "cancel" else "failed"
        self.assertEqual(f.detail(run)["state"], expected)
        with f.sessions() as db:
            job = db.scalar(select(RuntimeJob))
            self.assertEqual(
                job.state, "cancelled" if mode == "cancel" else "timed-out"
            )
            self.assertTrue(job.runtime_observed)
        self.assertFalse(self.failures)
        self.assertTrue(all(len(ids) == 1 for ids in self.created.values()))
        self.evidence(mode, [run], self.engine_states([run]))

    def test_real_job_cancellation_survives_submitting_worker_loss(self):
        self.terminal_after_worker_loss("cancel")

    def test_real_job_deadline_survives_submitting_worker_loss(self):
        self.terminal_after_worker_loss("deadline")
