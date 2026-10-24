"""Assignment-authoritative history, accounting and delayed spool ingestion."""

import time
import uuid
import unittest
import test_api as fixtures
import test_dynamic_api as runtime
from app import database as models


class ProjectUsageTests(unittest.TestCase):
    setUp = fixtures.ControlPlaneTests.setUp
    tearDown = fixtures.ControlPlaneTests.tearDown
    setup_graph = runtime.DynamicApiTests.setup_graph
    publish = runtime.DynamicApiTests.publish
    worker = runtime.DynamicApiTests.worker
    arrange = runtime.DynamicApiTests.arrange
    builtin_claim = runtime.DynamicApiTests.builtin_claim

    def event(self, **payload):
        return {
            "id": str(uuid.uuid4()),
            "kind": "model.completed",
            "occurred_at": time.time(),
            "payload": {
                "provider": "openai",
                "model": "unpriced-fixture",
                "call_id": str(uuid.uuid4()),
                "input_tokens": 0,
                "reported_cost_usd": 0.1,
                **payload,
            },
        }

    def send(self, claim, headers, events):
        return self.client.post(
            "/api/v1/shared-worker/assignments/" + claim["assignment_id"] + "/events",
            json={"lease_token": claim["lease_token"], "events": events},
            headers=headers,
        )

    def test_delayed_history_uses_stored_assignment_scope_and_redacts_revoked_grant(
        self,
    ):
        scope, run_id, invocation, claim, headers, context = self.builtin_claim()
        event = self.event(
            conversation={"request": "private-github", "response": "answer"}
        )
        with self.sessions.begin() as db:
            assignment = db.get(models.Assignment, claim["assignment_id"])
            assignment.expires_at, assignment.status = 0, "completed"
            db.get(models.StageAttempt, invocation.execution_id).state = "completed"
            secret = db.scalar(
                fixtures.select(models.SecretVersion).where(
                    models.SecretVersion.project_id == scope.project_id,
                    models.SecretVersion.purpose == "github",
                )
            )
            secret.revoked_at = time.time()
        response = self.send(claim, headers, [event])
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.send(claim, headers, [event]).status_code, 202)
        duplicate = {
            **event,
            "id": str(uuid.uuid4()),
            "payload": dict(reversed(list(event["payload"].items()))),
        }
        self.assertEqual(self.send(claim, headers, [duplicate]).status_code, 202)
        base = f"/api/v1/projects/{scope.project_id}"
        events = self.client.get(
            base + "/events?repository_id=" + context.repository_id
        ).json()
        self.assertEqual(len(events), 1)
        row = events[0]
        self.assertEqual(row["run_id"], run_id)
        self.assertEqual(row["execution_id"], invocation.execution_id)
        self.assertEqual(row["assignment_id"], claim["assignment_id"])
        self.assertEqual(row["stage"], context.stage_id)
        self.assertEqual(row["workflow_revision_id"], context.workflow_revision_id)
        usage = self.client.get(
            base + "/usage?repository_id=" + context.repository_id
        ).json()
        self.assertEqual(usage["calls"], 1)
        self.assertEqual(usage["input_tokens"], 0)
        self.assertIsNone(usage["output_tokens"])
        self.assertIsNone(usage["estimated_usd"])
        self.assertEqual(usage["reported_usd"], 0.1)
        self.assertEqual(usage["unpriced_calls"], 1)
        self.assertEqual(usage["missing_usage_calls"], 1)
        self.assertEqual(
            self.client.get(base + "/usage?repository_id=foreign").json()["calls"], 0
        )
        conversation = self.client.get(
            base + "/model-calls/" + event["id"] + "/conversation"
        )
        self.assertEqual(conversation.status_code, 200, conversation.text)
        self.assertNotIn("private-github", conversation.text)
        foreign, _, _ = self.arrange("foreign")
        self.assertEqual(
            self.client.get(
                f"/api/v1/projects/{foreign.project_id}/model-calls/{event['id']}/conversation"
            ).status_code,
            404,
        )
        self.assertEqual(self.client.get("/api/v1/events").json(), [])

    def test_pre_upgrade_pinned_context_receives_authoritative_new_attribution(self):
        from app.project_context import decrypt, encrypt

        scope, _, _, claim, headers, _ = self.builtin_claim()
        with self.sessions.begin() as db:
            assignment = db.get(models.Assignment, claim["assignment_id"])
            pin = db.get(models.ActionContextSnapshot, assignment.context_id)
            document = decrypt(pin.ciphertext, self.settings)
            for key in ("definition_id", "executor", "job_id", "worker_id"):
                document["context"].pop(key, None)
            pin.ciphertext = encrypt(document, self.settings)
        response = self.client.post(
            "/api/v1/shared-worker/assignments/" + claim["assignment_id"] + "/context",
            json={"lease_token": claim["lease_token"]},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["context"]["definition_id"])
        self.assertEqual(response.json()["context"]["executor"], "builtin@1")
        self.assertEqual(
            response.json()["context"]["worker_id"], headers["X-Worker-Id"]
        )

    def test_two_active_projects_on_same_worker_do_not_share_history_or_calls(self):
        from app.projects import ProjectScope
        from factory.workflows.contracts import RunInvocation

        scope, alpha_run, _, claim, headers, _ = self.builtin_claim()
        with self.sessions() as db:
            pool_id = db.get(models.Assignment, claim["assignment_id"]).pool_id
        beta, base, repositories, _, graph = self.setup_graph("beta")
        self.assertEqual(
            self.client.put(
                base + "/worker-pools/" + pool_id, json={"enabled": True}
            ).status_code,
            200,
        )
        graph["stages"] = [{**graph["stages"][0], "pool_id": pool_id}]
        revision = self.publish(base, graph)
        runtime_service = self.app.state.dynamic_runtime
        runtime_service.store.enable_dynamic_repository(
            ProjectScope(beta), repositories["backend"]["id"]
        )
        response = self.client.post(
            base + "/workflow-runs",
            json={"revision_id": revision["revision_id"], "input": {}},
        )
        self.assertEqual(response.status_code, 202, response.text)
        beta_run = response.json()["id"]
        invocation = runtime_service.advance(
            RunInvocation(project_id=beta, run_id=beta_run)
        ).ready[0]
        response = self.client.post(
            "/api/v1/shared-worker/claim",
            json={**invocation.model_dump(), "claim_key": "beta-shared-worker-claim"},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        beta_claim = response.json()
        shared_call = str(uuid.uuid4())
        self.assertEqual(
            self.send(claim, headers, [self.event(call_id=shared_call)]).status_code,
            202,
        )
        self.assertEqual(
            self.send(
                beta_claim,
                headers,
                [self.event(call_id=shared_call, reported_cost_usd=0.2)],
            ).status_code,
            202,
        )
        for project, run, cost in (
            (scope.project_id, alpha_run, 0.1),
            (beta, beta_run, 0.2),
        ):
            result = self.client.get(
                f"/api/v1/projects/{project}/usage?worker_id=" + headers["X-Worker-Id"]
            ).json()
            self.assertEqual(result["calls"], 1)
            self.assertEqual(result["reported_usd"], cost)
            rows = self.client.get(
                f"/api/v1/projects/{project}/events?run_id=" + run
            ).json()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["run_id"], run)

    def test_post_redaction_budget_and_protocol_key_collisions_are_safe(self):
        from app.project_context import encrypt

        scope, _, _, claim, headers, _ = self.builtin_claim(deliver_context=False)
        with self.sessions.begin() as db:
            secret = db.scalar(
                fixtures.select(models.SecretVersion).where(
                    models.SecretVersion.project_id == scope.project_id,
                    models.SecretVersion.purpose == "github",
                )
            )
            secret.ciphertext = encrypt("input_tokens", self.settings)
        event = self.event(input_tokens=7, conversation={"request": "input_tokens"})
        response = self.send(claim, headers, [event])
        # No business context was delivered yet; no attributed capture is accepted.
        self.assertEqual(response.status_code, 409)
        path = (
            "/api/v1/shared-worker/assignments/" + claim["assignment_id"] + "/context"
        )
        self.assertEqual(
            self.client.post(
                path, json={"lease_token": claim["lease_token"]}, headers=headers
            ).status_code,
            200,
        )
        response = self.send(claim, headers, [event])
        self.assertEqual(response.status_code, 202, response.text)
        usage = self.client.get(f"/api/v1/projects/{scope.project_id}/usage").json()
        self.assertEqual(usage["input_tokens"], 7)
        with self.sessions.begin() as db:
            secret = db.scalar(
                fixtures.select(models.SecretVersion).where(
                    models.SecretVersion.project_id == scope.project_id,
                    models.SecretVersion.purpose == "github",
                )
            )
            secret.ciphertext = encrypt("z", self.settings)
        oversized = self.event(conversation={"request": ["z" * 5000] * 20})
        response = self.send(claim, headers, [oversized])
        self.assertEqual(response.status_code, 413, response.text)
        with self.sessions() as db:
            self.assertIsNone(db.get(models.Event, oversized["id"]))

    def test_shared_observer_spool_delivers_after_completion_over_real_http_routes(
        self,
    ):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from factory.shared_worker import (
            SharedControl,
            SharedSettings,
            SharedRequestError,
        )
        from factory.adapters.shared_history import SharedPublisher
        from factory.adapters.pricing import Pricing
        from factory.application.telemetry import (
            model_call,
            ModelUsage,
            record_conversation,
        )
        from factory.workflows.contracts import ActionResult

        scope, run_id, invocation, _, headers, _ = self.builtin_claim(
            perform_claim=False
        )
        with self.sessions() as db:
            worker = db.get(models.Worker, headers["X-Worker-Id"])
            pool = db.get(models.WorkerPool, worker.pool_id)
            settings = SharedSettings(
                worker_id=worker.id,
                worker_name=worker.name,
                pool_id=pool.id,
                **{key: value for key, value in pool.profile.items() if key != "name"},
            )
        control = SharedControl(
            settings,
            {
                "FACTORY_CONTROL_ENDPOINT": "http://control",
                "FACTORY_CONTROL_TOKEN": headers["Authorization"][7:],
            },
        )

        def request(path, body=None):
            response = self.client.post(path, json=body, headers=headers)
            if response.status_code >= 400:
                raise SharedRequestError(response.status_code)
            return response.json()

        def builtin(action, context, inputs, transport, *, observer):
            with observer.observe(
                "load_issue", {"repository": context.settings.repository}
            ):
                with model_call("openai", "unpriced-fixture") as call:
                    call.record(
                        ModelUsage(
                            input_tokens=0, output_tokens=2, reported_cost_usd=0.01
                        )
                    )
                    record_conversation(
                        request="private-github", response="bounded response"
                    )
            return ActionResult(
                output={"issue_number": 2, "state": "open", "labels": []}
            )

        with tempfile.TemporaryDirectory() as directory:
            control.history = SharedPublisher(control, Path(directory), start=False)
            with (
                patch.object(control, "request", side_effect=request),
                patch("factory.bootstrap.run_builtin", side_effect=builtin),
                patch("factory.adapters.control.Pricing", return_value=Pricing()),
            ):
                self.assertEqual(control.execute(invocation).state, "completed")
                self.assertTrue(list(Path(directory).rglob("*.json")))
                control.history.flush_once()
                self.assertEqual(list(Path(directory).rglob("*.json")), [])
            control.shutdown()
        base = f"/api/v1/projects/{scope.project_id}"
        rows = self.client.get(base + "/events?kind=model.completed").json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["worker_id"], headers["X-Worker-Id"])
        self.assertEqual(rows[0]["run_id"], run_id)
        self.assertEqual(rows[0]["execution_id"], invocation.execution_id)
        usage = self.client.get(base + "/usage").json()
        self.assertEqual(usage["input_tokens"], 0)
        self.assertEqual(usage["reported_usd"], 0.01)
        content = self.client.get(
            base + "/model-calls/" + rows[0]["id"] + "/conversation"
        ).json()
        self.assertEqual(content["request"], "[REDACTED]")

    def test_history_rejects_forged_attribution_and_wrong_assignment_proof(self):
        scope, run_id, _, claim, headers, _ = self.builtin_claim()
        forged = {**self.event(), "attribution": {"project_id": "foreign"}}
        self.assertEqual(self.send(claim, headers, [forged]).status_code, 409)
        self.assertEqual(
            self.send(
                {**claim, "lease_token": "wrong"}, headers, [self.event()]
            ).status_code,
            409,
        )
        self.assertEqual(
            self.send(
                claim, headers, [{**self.event(), "run_id": "foreign"}]
            ).status_code,
            409,
        )
        contradictory = {
            **self.event(),
            "run_id": "foreign",
            "attribution": {"run_id": run_id},
        }
        self.assertEqual(self.send(claim, headers, [contradictory]).status_code, 409)

        self.assertEqual(
            self.send(
                claim, headers, [{**self.event(), "kind": "modelXcompleted"}]
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.get(f"/api/v1/projects/{scope.project_id}/events").json(), []
        )

    def docker_case(self, usage=None, *, grant=None):
        import test_docker_jobs as jobs
        from app.job_contracts import JobCapture

        case = jobs.JobBrokerTests("test_same_key_durable_reattach_and_digest_conflict")
        case.__dict__.update(self.__dict__)
        case.arrange()
        if grant is not None:
            case.set_grant_value(grant)
        receipt = case.submit()
        case.broker.reconcile_once(case.jobs)
        case.jobs.jobs[receipt.job_id] = "exited"
        case.jobs.collect = lambda ref: JobCapture(
            output={"cpu_seconds": 100, "claimed_tokens": 999},
            stdout="tokens: 999",
            usage=[item.model_dump() for item in usage or []],
        )
        case.broker.reconcile_once(case.jobs)
        self.assertEqual(case.submit().state, "completed")
        self.app.state.dynamic_runtime.settle_jobs(case.scope, case.run.id)
        self.app.state.dynamic_runtime.settle_jobs(case.scope, case.run.id)
        return case, receipt

    def test_external_job_declared_usage_is_attributed_and_counted_once(self):
        from factory.workflows.contracts import UsageRecord

        case, receipt = self.docker_case(
            [
                UsageRecord(
                    id="reported-call",
                    provider="external",
                    model="fixture",
                    input_tokens=0,
                    output_tokens=5,
                    reported_cost_usd=0.25,
                ),
                UsageRecord(id="unknown-call", provider="external", model="unknown"),
            ]
        )
        base = f"/api/v1/projects/{case.scope.project_id}"
        result = self.client.get(base + "/usage?job_id=" + receipt.job_id).json()
        self.assertEqual(result["calls"], 2)
        self.assertEqual(result["input_tokens"], 0)
        self.assertEqual(result["output_tokens"], 5)
        self.assertEqual(result["reported_usd"], 0.25)
        self.assertIsNone(result["estimated_usd"])
        self.assertEqual(result["unpriced_calls"], 2)
        self.assertEqual(result["missing_usage_calls"], 1)
        events = self.client.get(
            base + "/events?kind=model.completed&executor=docker@1"
        ).json()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["job_id"], receipt.job_id)
        self.assertEqual(events[0]["execution_id"], case.execution)
        self.assertNotIn("private-job-key", str(events))
        self.assertEqual(
            self.client.get(base + "/usage?provider=other").json()["calls"], 0
        )

    def test_broker_usage_protocol_keys_survive_grant_collisions(self):
        from factory.workflows.contracts import UsageRecord

        case, _ = self.docker_case(
            [
                UsageRecord(
                    id="call", provider="external", model="fixture", input_tokens=7
                )
            ],
            grant="input_tokens",
        )
        usage = self.client.get(
            f"/api/v1/projects/{case.scope.project_id}/usage"
        ).json()
        self.assertEqual(usage["input_tokens"], 7)
        with self.sessions() as db:
            self.assertEqual(
                db.get(models.StageAttempt, case.execution).state, "completed"
            )

    def test_new_job_receives_only_authoritative_public_execution_context(self):
        case, receipt = self.docker_case()
        context = case.jobs.launches[0].context
        self.assertEqual(context["project_id"], case.scope.project_id)
        self.assertEqual(context["run_id"], case.run.id)
        self.assertEqual(context["execution_id"], case.execution)
        self.assertEqual(context["job_id"], receipt.job_id)
        self.assertEqual(context["executor"], "docker@1")
        self.assertTrue(context["definition_id"])
        self.assertNotIn("private-job-key", str(context))
        self.assertNotIn("credentials", context)

    def test_job_context_file_is_read_only_and_separate_from_business_input(self):
        import json
        from pathlib import Path
        import test_docker_jobs as jobs
        from app.job_contracts import JobLaunch

        case = jobs.DockerJobRuntimeTests(
            "test_create_policy_and_same_identity_reattach"
        )
        case.setUp()
        self.addCleanup(case.doCleanups)
        context = {"project_id": "p", "execution_id": "e", "job_id": case.launch.job_id}
        launch = JobLaunch.model_validate(
            {**case.launch.model_dump(), "context": context}
        )
        case.runtime.ensure(launch)
        directory = Path(case.tmp.name) / launch.job_id
        self.assertEqual(json.loads((directory / "context.json").read_text()), context)
        self.assertEqual(
            json.loads((directory / "input.json").read_text()), launch.inputs
        )
        volume = case.client.containers.create.call_args.kwargs["volumes"][
            str(directory / "context.json")
        ]
        self.assertEqual(volume, {"bind": "/flumic/context.json", "mode": "ro"})

    def test_job_cpu_logs_and_missing_usage_do_not_become_zero_ai_cost(self):
        case, _ = self.docker_case()
        result = self.client.get(
            f"/api/v1/projects/{case.scope.project_id}/usage"
        ).json()
        self.assertEqual(result["calls"], 0)
        self.assertIsNone(result["input_tokens"])
        self.assertIsNone(result["estimated_usd"])
        self.assertIsNone(result["reported_usd"])

    def capture_case(self, mode):
        import json
        from app.project_context import decrypt, encrypt

        scope, _, _, claim, headers, _ = self.builtin_claim(deliver_context=False)
        with self.sessions.begin() as db:
            assignment = db.get(models.Assignment, claim["assignment_id"])
            pin = db.get(models.ActionContextSnapshot, assignment.context_id)
            document = decrypt(pin.ciphertext, self.settings)
            settings = json.loads(document["context"]["settings_json"])
            settings["conversation_capture"] = mode
            document["context"]["settings_json"] = json.dumps(settings)
            pin.ciphertext = encrypt(document, self.settings)
        self.assertEqual(
            self.client.post(
                "/api/v1/shared-worker/assignments/"
                + claim["assignment_id"]
                + "/context",
                json={"lease_token": claim["lease_token"]},
                headers=headers,
            ).status_code,
            200,
        )
        event = self.event(
            conversation={"request": "must not retain"}, conversation_available=True
        )
        response = self.send(claim, headers, [event])
        self.assertEqual(response.status_code, 202, response.text)
        return scope, event

    def test_metadata_only_capture_policy_is_enforced_from_pinned_context(self):
        scope, event = self.capture_case("metadata_only")
        base = f"/api/v1/projects/{scope.project_id}"
        rows = self.client.get(base + "/events").json()
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["payload"].get("conversation_available", False))
        self.assertEqual(
            self.client.get(
                base + "/model-calls/" + event["id"] + "/conversation"
            ).status_code,
            404,
        )

    def test_disabled_model_history_cannot_be_reenabled_by_worker_payload(self):
        scope, _ = self.capture_case("disabled")
        self.assertEqual(
            self.client.get(f"/api/v1/projects/{scope.project_id}/events").json(), []
        )
        self.assertEqual(
            self.client.get(f"/api/v1/projects/{scope.project_id}/usage").json()[
                "calls"
            ],
            0,
        )

    def test_shared_heartbeat_is_infrastructure_scoped_and_cannot_claim_project(self):
        _, _, _, _, headers, _ = self.builtin_claim()
        response = self.client.post(
            "/api/v1/shared-worker/heartbeat", json={}, headers=headers
        )
        self.assertEqual(response.status_code, 202, response.text)
        with self.sessions() as db:
            worker = db.get(models.Worker, headers["X-Worker-Id"])
            self.assertIsNotNone(worker.last_heartbeat_at)
            self.assertEqual(list(db.scalars(fixtures.select(models.Event))), [])
        self.assertEqual(
            self.client.post(
                "/api/v1/shared-worker/heartbeat",
                json={"project_id": "forged"},
                headers=headers,
            ).status_code,
            422,
        )


class HistoryMigrationTests(unittest.TestCase):
    def test_populated_history_upgrade_preserves_legacy_identity_and_unknown_attribution(
        self,
    ):
        import os
        import tempfile
        from unittest.mock import patch
        from alembic import command
        from alembic.config import Config
        from sqlalchemy import create_engine, text, inspect
        from sqlalchemy.engine import make_url

        with tempfile.TemporaryDirectory() as directory:
            urls = ["sqlite:///" + directory + "/history.sqlite3"]
            if os.environ.get("CONTROL_MIGRATION_TEST_URL"):
                urls.append(os.environ["CONTROL_MIGRATION_TEST_URL"])
            for original in urls:
                admin = None
                schema = None
                url = original
                if original.startswith("postgresql"):
                    admin = create_engine(original)
                    schema = "history_migration_" + uuid.uuid4().hex
                    with admin.begin() as db:
                        db.execute(text('CREATE SCHEMA "' + schema + '"'))
                    url = (
                        make_url(original)
                        .update_query_dict({"options": "-csearch_path=" + schema})
                        .render_as_string(hide_password=False)
                    )
                engine = None
                try:
                    cfg = Config(
                        str(fixtures.ROOT / "control-plane/backend/alembic.ini")
                    )
                    with patch.dict(os.environ, {"CONTROL_DATABASE_URL": url}):
                        command.upgrade(cfg, "e201f1000008")
                        engine = create_engine(url)
                        with engine.begin() as db:
                            db.execute(
                                text(
                                    "INSERT INTO execution_events(id,project_id,kind,worker_id,occurred_at,received_at,payload) VALUES ('legacy-event',:project,'model.completed','legacy-worker',1,1,:payload)"
                                ),
                                {
                                    "project": models.DEFAULT_PROJECT_ID,
                                    "payload": '{"input_tokens":0}',
                                },
                            )
                            db.execute(
                                text(
                                    "INSERT INTO conversation_content(id,project_id,ciphertext,expires_at) VALUES ('legacy-event',:project,'preserved-ciphertext',123)"
                                ),
                                {"project": models.DEFAULT_PROJECT_ID},
                            )
                        command.upgrade(cfg, "e201f1000009")
                        self.assertIn(
                            "execution_id",
                            {
                                column["name"]
                                for column in inspect(engine).get_columns(
                                    "execution_events"
                                )
                            },
                        )
                        with engine.connect() as db:
                            row = db.execute(
                                text(
                                    "SELECT id,execution_id,definition_id,executor FROM execution_events"
                                )
                            ).one()
                            self.assertEqual(
                                tuple(row), ("legacy-event", None, None, None)
                            )
                            self.assertEqual(
                                db.execute(
                                    text("SELECT ciphertext FROM conversation_content")
                                ).scalar(),
                                "preserved-ciphertext",
                            )
                        command.downgrade(cfg, "e201f1000008")
                        self.assertNotIn(
                            "execution_id",
                            {
                                column["name"]
                                for column in inspect(engine).get_columns(
                                    "execution_events"
                                )
                            },
                        )
                        command.upgrade(cfg, "e201f1000009")
                finally:
                    if engine:
                        engine.dispose()
                    if admin:
                        with admin.begin() as db:
                            db.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
                        admin.dispose()
