"""Project run lifecycle and assignment-only runtime access over real HTTP routes."""

import time
import unittest
from unittest.mock import Mock
import test_api as fixtures
import test_workflow_state as state_fixtures
from app import database as models
from factory.workflows.contracts import RunInvocation


class DynamicApiTests(unittest.TestCase):
    setUp = fixtures.ControlPlaneTests.setUp
    tearDown = fixtures.ControlPlaneTests.tearDown
    setup_graph = state_fixtures.WorkflowStateTests.setup_graph
    publish = state_fixtures.WorkflowStateTests.publish
    worker = state_fixtures.WorkflowStateTests.worker
    arrange = state_fixtures.WorkflowStateTests.arrange

    def launch(self, scope, definition, **changes):
        return self.client.post(
            f"/api/v1/projects/{scope.project_id}/workflow-runs",
            json={
                "revision_id": definition["revision_id"],
                "input": {"message": "private input"},
                **changes,
            },
        )

    def test_launch_idempotent_pinned_scoped_and_ids_only_engine_outbox(self):
        scope, _, definition = self.arrange()
        response = self.launch(scope, definition)
        self.assertEqual(response.status_code, 202, response.text)
        identifier = response.json()["id"]
        self.assertEqual(response.json()["state"], "queued")
        self.assertEqual(self.launch(scope, definition).json()["id"], identifier)
        self.assertEqual(
            self.launch(scope, definition, input={"message": "changed"}).status_code,
            409,
        )
        self.workflow.submit_dynamic.return_value = {"run_id": "engine-identity"}
        self.app.state.dynamic_runtime.reconcile_outbox(self.workflow)
        payload = self.workflow.submit_dynamic.call_args.args[1]
        self.assertEqual(
            payload, {"project_id": scope.project_id, "run_id": identifier}
        )
        self.assertNotIn("private input", str(self.workflow.submit_dynamic.call_args))
        detail = self.client.get(
            f"/api/v1/projects/{scope.project_id}/workflow-runs/{identifier}"
        )
        self.assertEqual(detail.status_code, 200)
        self.assertIn("snapshot_id", detail.json())
        foreign, _, _ = self.arrange("beta")
        self.assertEqual(
            self.client.get(
                f"/api/v1/projects/{foreign.project_id}/workflow-runs/{identifier}"
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                f"/api/v1/projects/{foreign.project_id}/workflow-runs/{identifier}/cancel",
                json={},
            ).status_code,
            404,
        )

    def test_engine_failure_retains_accepted_intent_and_cancel_is_idempotent(self):
        scope, _, definition = self.arrange()
        launched = self.launch(scope, definition)
        self.assertEqual(launched.status_code, 202, launched.text)
        identifier = launched.json()["id"]
        self.workflow.submit_dynamic.side_effect = RuntimeError("private engine config")
        self.app.state.dynamic_runtime.reconcile_outbox(self.workflow)
        url = f"/api/v1/projects/{scope.project_id}/workflow-runs/{identifier}"
        detail = self.client.get(url).json()
        self.assertEqual(detail["dispatch"]["state"], "pending")
        self.assertNotIn("private engine config", str(detail))
        self.assertEqual(self.client.post(url + "/cancel", json={}).status_code, 202)
        self.assertEqual(
            self.client.post(url + "/cancel", json={}).json()["state"], "cancelled"
        )

    def test_echo_claim_result_completes_run_without_cross_scope_or_raw_context(self):
        scope, _, definition = self.arrange()
        response = self.launch(scope, definition)
        self.assertEqual(response.status_code, 202, response.text)
        run_id = response.json()["id"]
        frontier = self.app.state.dynamic_runtime.advance(
            RunInvocation(project_id=scope.project_id, run_id=run_id)
        )
        invocation = frontier.ready[0]
        with self.sessions() as db:
            worker = db.scalar(
                fixtures.select(models.Worker).where(
                    models.Worker.pool_id
                    == definition.get(
                        "pool_id",
                        db.get(
                            models.Assignment,
                            db.get(
                                models.StageAttempt, invocation.execution_id
                            ).assignment_id,
                        ).pool_id,
                    )
                )
            )
            headers = {
                "X-Worker-Id": worker.id,
                "Authorization": "Bearer "
                + fixtures.worker_token(self.settings, worker.id),
            }
        claimed = self.client.post(
            "/api/v1/shared-worker/claim",
            json={**invocation.model_dump(), "claim_key": "http-echo-claim-123"},
            headers=headers,
        )
        self.assertEqual(claimed.status_code, 200, claimed.text)
        value = claimed.json()
        self.assertEqual(value["executor"], "builtin@1")
        url = "/api/v1/shared-worker/assignments/" + value["assignment_id"]
        forged = self.client.post(
            url + "/context",
            json={"lease_token": value["lease_token"], "project_id": "foreign"},
            headers=headers,
        )
        self.assertEqual(forged.status_code, 422)
        complete = self.client.post(
            url + "/complete",
            json={
                "lease_token": value["lease_token"],
                "result": {"output": {"done": True}},
            },
            headers=headers,
        )
        self.assertEqual(complete.status_code, 200, complete.text)
        self.assertEqual(complete.json()["state"], "completed")
        self.assertEqual(
            self.app.state.dynamic_runtime.advance(
                RunInvocation(project_id=scope.project_id, run_id=run_id)
            ).state,
            "completed",
        )

    def claim_run(self, scope, definition):
        response = self.launch(scope, definition)
        self.assertEqual(response.status_code, 202, response.text)
        run_id = response.json()["id"]
        invocation = self.app.state.dynamic_runtime.advance(
            RunInvocation(project_id=scope.project_id, run_id=run_id)
        ).ready[0]
        with self.sessions() as db:
            attempt = db.get(models.StageAttempt, invocation.execution_id)
            row = db.get(models.Assignment, attempt.assignment_id)
            worker = db.scalar(
                fixtures.select(models.Worker).where(
                    models.Worker.pool_id == row.pool_id
                )
            )
            headers = {
                "X-Worker-Id": worker.id,
                "Authorization": "Bearer "
                + fixtures.worker_token(self.settings, worker.id),
            }
        claimed = self.client.post(
            "/api/v1/shared-worker/claim",
            json={**invocation.model_dump(), "claim_key": "runtime-http-proof-123"},
            headers=headers,
        )
        self.assertEqual(claimed.status_code, 200, claimed.text)
        return run_id, claimed.json(), headers

    def test_assignment_state_and_provenance_are_server_derived(self):
        scope, _, definition = self.arrange()
        _, claim, headers = self.claim_run(scope, definition)
        path = "/api/v1/shared-worker/assignments/" + claim["assignment_id"]
        proof = {"lease_token": claim["lease_token"]}
        write = self.client.post(
            path + "/state",
            json={**proof, "operation": "put", "key": "example", "value": {"count": 3}},
            headers=headers,
        )
        self.assertEqual(write.status_code, 200, write.text)
        value = self.client.post(
            path + "/state",
            json={**proof, "operation": "get", "key": "example"},
            headers=headers,
        )
        self.assertEqual(value.json(), {"value": {"count": 3}})
        forged = self.client.post(
            path + "/state",
            json={
                **proof,
                "operation": "get",
                "key": "example",
                "repository_id": "foreign",
            },
            headers=headers,
        )
        self.assertEqual(forged.status_code, 422)
        badproof = self.client.post(
            path + "/state",
            json={"lease_token": "bad", "operation": "get", "key": "example"},
            headers=headers,
        )
        self.assertEqual(badproof.status_code, 409)
        context = self.client.post(path + "/context", json=proof, headers=headers)
        self.assertEqual(context.status_code, 200, context.text)
        self.assertEqual(context.json()["origins"], [])
        self.assertEqual(
            self.client.post(
                path + "/resource",
                json={**proof, "resource": "github.com/foreign/repo/issue/2"},
                headers=headers,
            ).status_code,
            404,
        )

    def test_cancel_started_builtin_fences_mutations_but_accepts_terminal_proof(self):
        scope, run_id, _, claim, headers, context = self.builtin_claim()
        url = f"/api/v1/projects/{scope.project_id}/workflow-runs/{run_id}"
        self.assertEqual(
            self.client.post(url + "/cancel", json={}).json()["state"], "cancelling"
        )
        path = "/api/v1/shared-worker/assignments/" + claim["assignment_id"]
        proof = {"lease_token": claim["lease_token"]}
        for endpoint, data in (
            ("renew", {}),
            ("context", {}),
            (
                "resource",
                {"resource": "github.com/phudekar/software-factory-test/issue/999"},
            ),
            ("state", {"operation": "put", "key": "new-after-cancel", "value": True}),
            ("followups", {"request": self.followup(context)}),
        ):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(
                    self.client.post(
                        path + "/" + endpoint, json={**proof, **data}, headers=headers
                    ).status_code,
                    409,
                )
        response = self.client.post(
            path + "/complete",
            json={**proof, "result": {"output": {"stopped": True}}},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get(url).json()["state"], "cancelled")

    def test_reconcile_completed_source_requeues_wait_with_original_pin(self):
        scope, run_id, _, claim, headers, context = self.builtin_claim()
        path = "/api/v1/shared-worker/assignments/" + claim["assignment_id"]
        response = self.client.post(
            path + "/followups",
            json={
                "lease_token": claim["lease_token"],
                "request": self.followup(context, kind="discussion", number=2),
            },
            headers=headers,
        )
        wait_id = response.json()["followup_id"]
        with self.sessions.begin() as db:
            db.get(models.StageAttempt, claim["execution_id"]).state = "completed"
            db.get(models.WorkflowRun, run_id).state = "completed"
        runtime = self.app.state.dynamic_runtime
        runtime.reconcile_followups()
        with self.sessions.begin() as db:
            row = db.scalar(
                fixtures.select(models.DynamicDispatch).where(
                    models.DynamicDispatch.subject_id == wait_id
                )
            )
            row.state, row.engine_id = "submitted", "failed-engine-wait"
        url = f"/api/v1/projects/{scope.project_id}/workflow-runs/{run_id}"
        response = self.client.post(url + "/reconcile", json={})
        self.assertEqual(response.status_code, 202, response.text)
        with self.sessions() as db:
            row = db.scalar(
                fixtures.select(models.DynamicDispatch).where(
                    models.DynamicDispatch.subject_id == wait_id
                )
            )
            self.assertEqual(row.state, "pending")
            self.assertEqual(row.payload, {"wait_id": wait_id})
        detail = self.client.get(url).json()
        self.assertEqual(detail["followups"][0]["id"], wait_id)
        self.assertEqual(
            detail["followups"][0]["dispatch"]["engine_id"], "failed-engine-wait"
        )

    def test_cancel_claimed_builtin_waits_for_terminal_result(self):
        scope, _, definition = self.arrange()
        run_id, claim, headers = self.claim_run(scope, definition)
        url = f"/api/v1/projects/{scope.project_id}/workflow-runs/{run_id}"
        self.assertEqual(
            self.client.post(url + "/cancel", json={}).json()["state"], "cancelling"
        )
        path = "/api/v1/shared-worker/assignments/" + claim["assignment_id"]
        completed = self.client.post(
            path + "/complete",
            json={
                "lease_token": claim["lease_token"],
                "result": {"output": {"done": True}},
            },
            headers=headers,
        )
        self.assertEqual(completed.status_code, 200, completed.text)
        self.assertEqual(self.client.get(url).json()["state"], "cancelled")

    def test_docker_job_receipt_is_not_a_completed_stage_and_worker_cannot_forge_output(
        self,
    ):
        import test_docker_jobs as jobs

        case = jobs.JobBrokerTests("test_same_key_durable_reattach_and_digest_conflict")
        # Use this HTTP app/database, with a real current-time lease.
        case.__dict__.update(self.__dict__)
        case.arrange()
        with self.sessions.begin() as db:
            row = db.get(models.Assignment, case.assignment)
            row.expires_at = time.time() + 120
            row.claim_hash = "server-bound-proof"
            worker = db.get(models.Worker, row.worker_id)
            from app.worker_pools import lease_token

            proof = lease_token(self.settings, row)
            headers = {
                "X-Worker-Id": worker.id,
                "Authorization": "Bearer "
                + fixtures.worker_token(self.settings, worker.id),
            }
        path = "/api/v1/shared-worker/assignments/" + case.assignment
        data = {"lease_token": proof, "spec_digest": case.digest}
        accepted = self.client.post(path + "/jobs", json=data, headers=headers)
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertEqual(accepted.json()["state"], "pending")
        self.assertEqual(
            self.client.post(path + "/jobs", json=data, headers=headers).json()[
                "job_id"
            ],
            accepted.json()["job_id"],
        )
        fake = self.client.post(
            path + "/complete",
            json={"lease_token": proof, "result": {"output": {"forged": True}}},
            headers=headers,
        )
        self.assertEqual(fake.status_code, 409)
        self.assertEqual(
            self.client.post(
                path + "/jobs", json={**data, "image": "other"}, headers=headers
            ).status_code,
            422,
        )
        runtime = self.app.state.dynamic_runtime
        broker = runtime.broker
        broker.reconcile_once(case.jobs)
        case.jobs.jobs[accepted.json()["job_id"]] = "exited"
        broker.reconcile_once(case.jobs)
        runtime.advance(
            RunInvocation(project_id=case.scope.project_id, run_id=case.run.id)
        )
        with self.sessions() as db:
            self.assertEqual(
                db.get(models.StageAttempt, case.execution).state, "completed"
            )

    def builtin_claim(self, *, deliver_context=True, perform_claim=True):
        import test_builtin_bindings as bundles

        case = bundles.BuiltinBindingTests(
            "test_seed_is_idempotent_and_pins_all_three_followup_workflows"
        )
        case.__dict__.update(self.__dict__)
        scope, base, repos, pools, values = case.setup_bundle(credentials=True)
        worker_id, headers = self.worker({"id": pools["simple"]}, "catalog-worker")
        with self.sessions.begin() as db:
            worker = db.get(models.Worker, worker_id)
            worker.capabilities = {
                **worker.capabilities,
                "actions": list(
                    db.get(models.WorkerPool, pools["simple"]).profile["actions"]
                ),
            }
        self.app.state.dynamic_runtime.store.enable_dynamic_repository(
            scope, repos["backend"]["id"]
        )
        response = self.client.post(
            base + "/workflow-runs",
            json={
                "revision_id": values["issue"],
                "input": {"issue_number": 2},
                "bindings": {repos["backend"]["id"]: values},
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        run_id = response.json()["id"]
        invocation = self.app.state.dynamic_runtime.advance(
            RunInvocation(project_id=scope.project_id, run_id=run_id)
        ).ready[0]
        if not perform_claim:
            return scope, run_id, invocation, None, headers, None
        claim = self.client.post(
            "/api/v1/shared-worker/claim",
            json={**invocation.model_dump(), "claim_key": "builtin-http-claim-123"},
            headers=headers,
        )
        self.assertEqual(claim.status_code, 200, claim.text)
        path = "/api/v1/shared-worker/assignments/" + claim.json()["assignment_id"]
        if not deliver_context:
            return scope, run_id, invocation, claim.json(), headers, None
        delivered = self.client.post(
            path + "/context",
            json={"lease_token": claim.json()["lease_token"]},
            headers=headers,
        ).json()
        from factory.workflows.contracts import ActionContext
        import json

        context = ActionContext.model_validate_json(
            json.dumps(delivered["context"])
        ).with_credentials(delivered["credentials"])
        return scope, run_id, invocation, claim.json(), headers, context

    def followup(self, context, kind="issue", number=7):
        from factory.workflows.project_dispatch import ProjectBindings, ProjectDispatch
        from factory.domain.models import IssueInput

        class Capture:
            def submit_followup(self, context, request):
                self.value = request
                return True

        capture = Capture()
        ProjectDispatch(
            context,
            ProjectBindings.model_validate_json(context.followup_bindings_json),
            capture,
        )._request(
            kind,
            IssueInput(repository=context.settings.repository, issue_number=number),
        )
        return capture.value.model_dump(mode="json")

    def test_followups_accept_only_pinned_targets_and_replay_after_source_completion(
        self,
    ):
        scope, run_id, invocation, claim, headers, context = self.builtin_claim()
        path = "/api/v1/shared-worker/assignments/" + claim["assignment_id"]
        value = self.followup(context)
        body = {"lease_token": claim["lease_token"], "request": value}
        accepted = self.client.post(path + "/followups", json=body, headers=headers)
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertTrue(accepted.json()["accepted"])
        self.assertEqual(
            self.client.post(path + "/followups", json=body, headers=headers).json()[
                "followup_id"
            ],
            accepted.json()["followup_id"],
        )
        for mutation in (
            {"project_id": "foreign"},
            {"repository_id": "foreign"},
            {"source_execution_id": "foreign"},
            {"idempotency_key": "a" * 64},
            {"target": {"snapshot_id": "foreign", "workflow_revision_id": "foreign"}},
        ):
            with self.subTest(mutation=mutation):
                forged = self.client.post(
                    path + "/followups",
                    json={**body, "request": {**value, **mutation}},
                    headers=headers,
                )
                self.assertIn(forged.status_code, {404, 409, 422})
        runtime = self.app.state.dynamic_runtime
        runtime.reconcile_followups()
        with self.sessions() as db:
            self.assertEqual(
                len(list(db.scalars(fixtures.select(models.WorkflowRun)))), 1
            )
        completed = self.client.post(
            path + "/complete",
            json={
                "lease_token": claim["lease_token"],
                "result": {
                    "output": {"issue_number": 2, "state": "open", "labels": []}
                },
            },
            headers=headers,
        )
        self.assertEqual(completed.status_code, 200, completed.text)
        runtime.reconcile_followups()
        runtime.reconcile_followups()
        with self.sessions() as db:
            runs = list(db.scalars(fixtures.select(models.WorkflowRun)))
            self.assertEqual(len(runs), 2)
            child = next(r for r in runs if r.id != run_id)
            from app.project_context import decrypt

            self.assertEqual(
                decrypt(child.input_ciphertext, self.settings)["factory_run_id"],
                context.factory_run_id,
            )
            self.assertEqual(
                decrypt(child.input_ciphertext, self.settings)["issue_number"], 7
            )

    def test_revoked_wait_grant_records_blocked_without_fallback(self):
        scope, _, _, claim, headers, context = self.builtin_claim()
        path = "/api/v1/shared-worker/assignments/" + claim["assignment_id"]
        response = self.client.post(
            path + "/followups",
            json={
                "lease_token": claim["lease_token"],
                "request": self.followup(context, kind="discussion", number=2),
            },
            headers=headers,
        )
        self.assertEqual(response.status_code, 202, response.text)
        with self.sessions.begin() as db:
            db.get(models.StageAttempt, claim["execution_id"]).state = "completed"
            secret = db.scalar(
                fixtures.select(models.SecretVersion).where(
                    models.SecretVersion.project_id == scope.project_id,
                    models.SecretVersion.purpose == "github",
                )
            )
            secret.revoked_at = time.time()
        runtime = self.app.state.dynamic_runtime
        runtime.reconcile_followups()
        with self.assertRaisesRegex(
            ValueError, "Pinned wait requires operator reconciliation"
        ):
            runtime.check_wait(response.json()["followup_id"])
        with self.sessions() as db:
            self.assertEqual(
                db.get(models.ProjectFollowup, response.json()["followup_id"]).state,
                "blocked",
            )

    def test_completed_wait_replay_reports_ready_and_never_repolls(self):
        _, _, _, claim, headers, context = self.builtin_claim()
        path = "/api/v1/shared-worker/assignments/" + claim["assignment_id"]
        response = self.client.post(
            path + "/followups",
            json={
                "lease_token": claim["lease_token"],
                "request": self.followup(context, kind="discussion", number=2),
            },
            headers=headers,
        )
        identifier = response.json()["followup_id"]
        with self.sessions.begin() as db:
            db.get(models.ProjectFollowup, identifier).state = "completed"
        self.assertEqual(
            self.app.state.dynamic_runtime.check_wait(identifier).state, "ready"
        )
        self.assertTrue(self.app.state.dynamic_runtime.resume_wait(identifier))

    def test_wait_cancellation_does_not_call_github_or_resume(self):
        scope, run_id, _, claim, headers, context = self.builtin_claim()
        path = "/api/v1/shared-worker/assignments/" + claim["assignment_id"]
        response = self.client.post(
            path + "/followups",
            json={
                "lease_token": claim["lease_token"],
                "request": self.followup(context, kind="discussion", number=2),
            },
            headers=headers,
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.client.post(
            f"/api/v1/projects/{scope.project_id}/workflow-runs/{run_id}/cancel",
            json={},
        )
        status = self.app.state.dynamic_runtime.check_wait(
            response.json()["followup_id"]
        )
        self.assertEqual(status.state, "cancelled")
        self.assertFalse(
            self.app.state.dynamic_runtime.resume_wait(response.json()["followup_id"])
        )

    def test_engine_adapter_submits_typed_ids_and_handles_collision(self):
        from app.adapters.hatchet import HatchetEngine
        from factory.settings import Settings
        from hatchet_sdk.exceptions import IdempotencyCollisionError

        sdk = Mock()
        sdk.workflow.return_value.run.return_value.workflow_run_id = "engine-id"
        engine = HatchetEngine(Settings(), sdk)
        payload = {"project_id": "p", "run_id": "run"}
        self.assertEqual(
            engine.submit_dynamic("run", payload, "dispatch")["run_id"], "engine-id"
        )
        call = sdk.workflow.return_value.run.call_args
        self.assertEqual(call.args[0].model_dump(), payload)
        self.assertFalse(call.kwargs["wait_for_result"])
        self.assertEqual(
            sdk.workflow.call_args.kwargs["name"], "flumic-dynamic-v1-coordinate"
        )
        sdk.workflow.return_value.run.side_effect = IdempotencyCollisionError(
            existing_run_external_id="existing"
        )
        self.assertEqual(
            engine.submit_dynamic("run", payload, "dispatch")["run_id"], "existing"
        )
        with self.assertRaises(ValueError):
            engine.submit_dynamic("arbitrary-code", payload, "dispatch")
        with self.assertRaises(ValueError):
            engine.submit_dynamic(
                "run", {**payload, "input": {"secret": "no"}}, "dispatch"
            )

    def test_operator_reconcile_is_durable_and_project_owned(self):
        scope, _, definition = self.arrange()
        response = self.launch(scope, definition)
        self.assertEqual(response.status_code, 202, response.text)
        run_id = response.json()["id"]
        self.workflow.submit_dynamic.return_value = {"run_id": "first"}
        runtime = self.app.state.dynamic_runtime
        runtime.reconcile_outbox(self.workflow)
        url = f"/api/v1/projects/{scope.project_id}/workflow-runs/{run_id}"
        accepted = self.client.post(url + "/reconcile", json={})
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertEqual(accepted.json()["dispatch"]["state"], "pending")
        self.workflow.submit_dynamic.return_value = {"run_id": "second"}
        runtime.reconcile_outbox(self.workflow)
        self.assertEqual(self.client.get(url).json()["dispatch"]["engine_id"], "second")

    def test_repository_mode_and_builtin_seed_routes_validate_owned_configuration(self):
        import test_builtin_bindings as bundles

        case = bundles.BuiltinBindingTests(
            "test_seed_is_idempotent_and_pins_all_three_followup_workflows"
        )
        case.__dict__.update(self.__dict__)
        scope, base, repos, pools, values = case.setup_bundle()
        enabled = self.client.post(
            base + "/repositories/" + repos["backend"]["id"] + "/dynamic-mode", json={}
        )
        self.assertEqual(enabled.status_code, 200, enabled.text)
        self.assertEqual(enabled.json()["mode"], "dynamic")
        body = {
            "repository": "backend",
            "simple_pool": pools["simple"],
            "developer_pool": pools["developer"],
        }
        seeded = self.client.post(
            base + "/workflow-definitions/seed-builtins", json=body
        )
        self.assertEqual(seeded.status_code, 200, seeded.text)
        self.assertEqual(seeded.json(), values)
        self.assertEqual(
            self.client.post(
                base + "/repositories/foreign/dynamic-mode", json={}
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                base + "/workflow-definitions/seed-builtins",
                json={**body, "repository": "foreign"},
            ).status_code,
            404,
        )

    def test_closed_review_wait_records_state_without_model_credentials(self):
        from unittest.mock import patch

        scope, _, _, claim, headers, context = self.builtin_claim()
        from factory.domain.models import PullInput
        from factory.workflows.project_dispatch import ProjectDispatch, ProjectBindings

        class Capture:
            def submit_followup(self, context, request):
                self.value = request
                return True

        capture = Capture()
        ProjectDispatch(
            context,
            ProjectBindings.model_validate_json(context.followup_bindings_json),
            capture,
        ).wait_for_review(
            PullInput(
                repository=context.settings.repository,
                issue_number=2,
                pull_number=4,
                pull_url="https://github.com/"
                + context.settings.repository
                + "/pull/4",
                branch="agent/test",
            )
        )
        path = "/api/v1/shared-worker/assignments/" + claim["assignment_id"]
        response = self.client.post(
            path + "/followups",
            json={
                "lease_token": claim["lease_token"],
                "request": capture.value.model_dump(mode="json"),
            },
            headers=headers,
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.client.post(
            path + "/complete",
            json={
                "lease_token": claim["lease_token"],
                "result": {
                    "output": {"issue_number": 2, "state": "open", "labels": []}
                },
            },
            headers=headers,
        )
        runtime = self.app.state.dynamic_runtime
        runtime.reconcile_followups()
        github = Mock()
        github.get_pull_request.return_value = {"state": "closed", "merged": False}
        with patch("github_client.GitHubClient", return_value=github) as client:
            status = runtime.check_wait(response.json()["followup_id"])
        self.assertEqual(status.state, "closed-unmerged")
        with self.sessions() as db:
            self.assertEqual(
                db.get(models.ProjectFollowup, response.json()["followup_id"]).state,
                "closed-unmerged",
            )
        self.assertEqual(client.call_args.kwargs["token"], "private-github")
        self.assertEqual(
            runtime.store.get_state(
                scope,
                context.repository_id,
                "pull-lifecycle:v1:" + context.settings.repository + ":4",
            )["status"],
            "closed-unmerged",
        )

    def test_missing_run_outbox_repairs_amid_large_submitted_history(self):
        import uuid

        scope, _, definition = self.arrange()
        response = self.launch(scope, definition)
        run_id = response.json()["id"]
        with self.sessions.begin() as db:
            source = db.get(models.WorkflowRun, run_id)
            row = db.scalar(
                fixtures.select(models.DynamicDispatch).where(
                    models.DynamicDispatch.subject_id == run_id
                )
            )
            db.delete(row)
            for i in range(101):
                identifier = str(uuid.uuid4())
                db.add(
                    models.WorkflowRun(
                        id=identifier,
                        project_id=scope.project_id,
                        snapshot_id=source.snapshot_id,
                        sequence=i + 100,
                        request_key="history-" + str(i),
                        request_digest="a" * 64,
                        input_ciphertext=source.input_ciphertext,
                        state="queued",
                        created_at=0,
                    )
                )
                db.add(
                    models.DynamicDispatch(
                        project_id=scope.project_id,
                        kind="run",
                        subject_id=identifier,
                        payload={"project_id": scope.project_id, "run_id": identifier},
                        state="submitted",
                        engine_id="existing",
                    )
                )
        self.workflow.submit_dynamic.return_value = {"run_id": "repaired"}
        self.app.state.dynamic_runtime.reconcile_outbox(self.workflow)
        with self.sessions() as db:
            row = db.scalar(
                fixtures.select(models.DynamicDispatch).where(
                    models.DynamicDispatch.subject_id == run_id
                )
            )
            self.assertIsNotNone(row)

    def test_worker_failure_flag_cannot_release_uncertain_mutating_action(self):
        _, _, _, claim, headers, _ = self.builtin_claim()
        with self.sessions.begin() as db:
            db.get(models.Assignment, claim["assignment_id"]).action = "issue.plan@1"
        response = self.client.post(
            "/api/v1/shared-worker/assignments/" + claim["assignment_id"] + "/complete",
            json={
                "lease_token": claim["lease_token"],
                "failed": True,
                "result": {"output": {"error": "failed"}},
            },
            headers=headers,
        )
        self.assertEqual(response.status_code, 409, response.text)
        with self.sessions() as db:
            self.assertEqual(
                db.get(models.StageAttempt, claim["execution_id"]).state, "running"
            )

    def test_resolve_lost_builtin_requires_explicit_external_effect_confirmation(self):
        scope, _, definition = self.arrange()
        run_id, claim, _ = self.claim_run(scope, definition)
        url = f"/api/v1/projects/{scope.project_id}/workflow-runs/{run_id}/executions/{claim['execution_id']}/resolve"
        rejected = self.client.post(url, json={"state": "failed"})
        self.assertEqual(rejected.status_code, 422, rejected.text)
        response = self.client.post(
            url, json={"state": "failed", "confirmation": "external-effects-reconciled"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["state"], "failed")

    def test_cancel_before_context_delivery_prevents_builtin_start(self):
        scope, run_id, _, claim, headers, _ = self.builtin_claim(deliver_context=False)
        response = self.client.post(
            f"/api/v1/projects/{scope.project_id}/workflow-runs/{run_id}/cancel",
            json={},
        )
        self.assertEqual(response.json()["state"], "cancelled")
        path = "/api/v1/shared-worker/assignments/" + claim["assignment_id"]
        self.assertEqual(
            self.client.post(
                path + "/context",
                json={"lease_token": claim["lease_token"]},
                headers=headers,
            ).status_code,
            409,
        )

    def test_same_claim_can_acknowledge_stopped_execution_after_lease_expiry(self):
        scope, _, definition = self.arrange()
        _, claim, headers = self.claim_run(scope, definition)
        with self.sessions.begin() as db:
            db.get(models.Assignment, claim["assignment_id"]).expires_at = (
                time.time() - 1
            )
        path = "/api/v1/shared-worker/assignments/" + claim["assignment_id"]
        proof = {"lease_token": claim["lease_token"]}
        self.assertEqual(
            self.client.post(
                path + "/context", json=proof, headers=headers
            ).status_code,
            409,
        )
        response = self.client.post(
            path + "/complete",
            json={**proof, "result": {"output": {"done": True}}},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["state"], "completed")

    def test_builtin_deadline_and_server_redaction_use_stored_authority(self):
        scope, run_id, _, claim, headers, context = self.builtin_claim()
        from app.project_context import decrypt

        with self.sessions.begin() as db:
            row = db.get(models.Assignment, claim["assignment_id"])
            row.started_at = time.time() - 10000
            secret = db.scalar(
                fixtures.select(models.SecretVersion).where(
                    models.SecretVersion.project_id == scope.project_id,
                    models.SecretVersion.purpose == "github",
                )
            )
            secret.revoked_at = time.time()
        path = "/api/v1/shared-worker/assignments/" + claim["assignment_id"]
        response = self.client.post(
            path + "/complete",
            json={
                "lease_token": claim["lease_token"],
                "result": {
                    "output": {
                        "issue_number": 2,
                        "state": "open",
                        "labels": ["private-github"],
                    }
                },
            },
            headers=headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["state"], "failed")
        with self.sessions() as db:
            result = decrypt(
                db.get(models.StageAttempt, claim["execution_id"]).result_ciphertext,
                self.settings,
            )
            self.assertNotIn("private-github", str(result))

    def test_background_settles_jobs_without_the_original_worker_or_parent(self):
        import test_docker_jobs as jobs

        case = jobs.JobBrokerTests("test_same_key_durable_reattach_and_digest_conflict")
        case.__dict__.update(self.__dict__)
        case.arrange()
        runtime = self.app.state.dynamic_runtime
        receipt = case.submit()
        case.broker.reconcile_once(case.jobs)
        case.jobs.jobs[receipt.job_id] = "exited"
        case.broker.reconcile_once(case.jobs)
        runtime.reconcile_background()
        with self.sessions() as db:
            self.assertEqual(
                db.get(models.StageAttempt, case.execution).state, "completed"
            )

    def test_expired_unstarted_action_is_proven_safe_but_started_builtin_is_not_retried(
        self,
    ):
        scope, run_id, _, claim, headers, _ = self.builtin_claim(deliver_context=False)
        with self.sessions.begin() as db:
            db.get(models.Assignment, claim["assignment_id"]).expires_at = 0
        self.app.state.dynamic_runtime.reconcile_background()
        with self.sessions() as db:
            self.assertEqual(
                db.get(models.StageAttempt, claim["execution_id"]).state, "failed"
            )

    def test_started_expired_builtin_keeps_uncertain_execution_fenced(self):
        _, _, _, claim, _, _ = self.builtin_claim()
        with self.sessions.begin() as db:
            row = db.get(models.Assignment, claim["assignment_id"])
            self.assertIsNotNone(row.started_at)
            row.expires_at = 0
        self.app.state.dynamic_runtime.reconcile_background()
        with self.sessions() as db:
            self.assertEqual(
                db.get(models.StageAttempt, claim["execution_id"]).state, "running"
            )

    def test_blocked_followup_does_not_starve_other_intents(self):
        from app.project_context import encrypt

        _, _, _, claim, headers, context = self.builtin_claim()
        path = "/api/v1/shared-worker/assignments/" + claim["assignment_id"]
        ids = []
        for number in (7, 8):
            response = self.client.post(
                path + "/followups",
                json={
                    "lease_token": claim["lease_token"],
                    "request": self.followup(context, number=number),
                },
                headers=headers,
            )
            self.assertEqual(response.status_code, 202, response.text)
            ids.append(response.json()["followup_id"])
        with self.sessions.begin() as db:
            db.get(models.StageAttempt, claim["execution_id"]).state = "completed"
            db.get(models.ProjectFollowup, ids[0]).ciphertext = encrypt(
                {"private": "broken"}, self.settings
            )
        self.app.state.dynamic_runtime.reconcile_followups()
        with self.sessions() as db:
            self.assertEqual(db.get(models.ProjectFollowup, ids[0]).state, "blocked")
            self.assertEqual(db.get(models.ProjectFollowup, ids[1]).state, "completed")

    def test_control_worker_composition_has_fixed_handlers_and_no_project_payload(self):
        from app.dynamic_runtime import build_runtime_worker

        engine = Mock()
        engine.task.return_value = lambda fn: fn
        engine.durable_task.return_value = lambda fn: fn
        runtime = self.app.state.dynamic_runtime
        build_runtime_worker(engine, runtime, self.settings)
        call = engine.worker.call_args.kwargs
        self.assertEqual(
            call["labels"], {"factory_protocol": "shared-v1", "factory_role": "control"}
        )
        self.assertEqual(len(call["workflows"]), 5)
        self.assertNotIn("private", str(call))

    def test_real_shared_client_executes_load_and_plan_over_authenticated_http(self):
        self.real_shared_client_load()

    def test_read_only_load_failure_releases_execution_and_project_quota(self):
        self.real_shared_client_load(read_failure=True)

    def real_shared_client_load(self, *, read_failure=False):
        import io
        import urllib.error
        from urllib.parse import urlsplit
        from unittest.mock import patch
        from factory.shared_worker import SharedControl, SharedSettings

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
                **{k: v for k, v in pool.profile.items() if k != "name"},
            )
        client = SharedControl(
            settings,
            {
                "FACTORY_CONTROL_ENDPOINT": "http://control",
                "FACTORY_CONTROL_TOKEN": headers["Authorization"][7:],
            },
        )

        class Response:
            def __init__(self, raw):
                self.raw = io.BytesIO(raw)

            def read(self, size=-1):
                return self.raw.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        calls = []

        def urlopen(request, timeout):
            path = urlsplit(request.full_url).path
            calls.append(path)
            response = self.client.request(
                request.get_method(),
                path,
                content=request.data,
                headers=dict(request.header_items()),
            )
            if response.status_code >= 400:
                raise urllib.error.HTTPError(
                    request.full_url,
                    response.status_code,
                    "rejected",
                    {},
                    io.BytesIO(response.content),
                )
            return Response(response.content)

        github = Mock()
        github.get_issue.return_value = {
            "number": 2,
            "state": "open",
            "title": "Fixture",
            "body": "",
            "labels": [{"name": "agent-subtask"}],
        }
        if read_failure:
            from factory.domain.models import RepositoryError

            github.get_issue.side_effect = RepositoryError("404 private-github")
        with (
            patch("urllib.request.urlopen", side_effect=urlopen),
            patch("github_client.GitHubClient", return_value=github) as repository,
            patch(
                "factory.adapters.agents.openai_model",
                side_effect=AssertionError("No paid model expected"),
            ),
        ):
            client.register()
            receipt = client.execute(invocation)
            if read_failure:
                self.assertEqual(receipt.state, "failed")
                with self.sessions() as db:
                    self.assertEqual(
                        db.get(
                            models.Assignment,
                            db.get(
                                models.StageAttempt, invocation.execution_id
                            ).assignment_id,
                        ).status,
                        "failed",
                    )
                    self.assertNotIn(
                        "private-github",
                        str(
                            db.get(
                                models.StageAttempt, invocation.execution_id
                            ).result_digest
                        ),
                    )
                self.assertEqual(
                    self.app.state.dynamic_runtime.advance(
                        RunInvocation(project_id=scope.project_id, run_id=run_id)
                    ).state,
                    "failed",
                )
                return
            self.assertEqual(receipt.state, "completed")
            next_stage = self.app.state.dynamic_runtime.advance(
                RunInvocation(project_id=scope.project_id, run_id=run_id)
            ).ready[0]
            self.assertEqual(client.execute(next_stage).state, "completed")
        self.assertEqual(repository.call_args.kwargs["token"], "private-github")
        self.assertTrue(any(path.endswith("/state") for path in calls))
        self.assertTrue(any(path.endswith("/resource") for path in calls))
        with self.sessions() as db:
            self.assertEqual(
                db.get(models.StageAttempt, next_stage.execution_id).state, "completed"
            )

    def test_job_status_can_read_terminal_receipt_with_original_proof(self):
        import test_docker_jobs as jobs

        case = jobs.JobBrokerTests("test_same_key_durable_reattach_and_digest_conflict")
        case.__dict__.update(self.__dict__)
        case.arrange()
        from app.worker_pools import lease_token

        with self.sessions.begin() as db:
            row = db.get(models.Assignment, case.assignment)
            row.expires_at, row.claim_hash = time.time() + 120, "proof"
            proof = lease_token(self.settings, row)
            headers = {
                "X-Worker-Id": row.worker_id,
                "Authorization": "Bearer "
                + fixtures.worker_token(self.settings, row.worker_id),
            }
        receipt = self.client.post(
            "/api/v1/shared-worker/assignments/" + case.assignment + "/jobs",
            json={"lease_token": proof, "spec_digest": case.digest},
            headers=headers,
        ).json()
        broker = self.app.state.dynamic_runtime.broker
        broker.reconcile_once(case.jobs)
        case.jobs.jobs[receipt["job_id"]] = "exited"
        broker.reconcile_once(case.jobs)
        self.app.state.dynamic_runtime.reconcile_background()
        response = self.client.post(
            "/api/v1/shared-worker/assignments/"
            + case.assignment
            + "/jobs/"
            + receipt["job_id"]
            + "/status",
            json={"lease_token": proof},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["state"], "completed")

    def test_wait_resume_cannot_create_child_after_a_racing_source_cancel(self):
        from unittest.mock import patch
        import app.followups as followups

        scope, run_id, _, claim, headers, context = self.builtin_claim()
        path = "/api/v1/shared-worker/assignments/" + claim["assignment_id"]
        response = self.client.post(
            path + "/followups",
            json={
                "lease_token": claim["lease_token"],
                "request": self.followup(context, kind="discussion", number=2),
            },
            headers=headers,
        )
        self.client.post(
            path + "/complete",
            json={
                "lease_token": claim["lease_token"],
                "result": {
                    "output": {"issue_number": 2, "state": "open", "labels": []}
                },
            },
            headers=headers,
        )
        runtime = self.app.state.dynamic_runtime
        runtime.reconcile_followups()
        original = followups._waiting

        def cancel_between_check_and_launch(*args):
            record = original(*args)
            runtime.cancel(scope, run_id)
            return record

        with patch.object(
            followups, "_waiting", side_effect=cancel_between_check_and_launch
        ):
            self.assertFalse(runtime.resume_wait(response.json()["followup_id"]))
        with self.sessions() as db:
            self.assertEqual(
                len(list(db.scalars(fixtures.select(models.WorkflowRun)))), 1
            )

    def test_followup_validation_and_trace_do_not_expose_granted_credentials(self):

        scope, _, _, claim, headers, context = self.builtin_claim()
        path = (
            "/api/v1/shared-worker/assignments/" + claim["assignment_id"] + "/followups"
        )
        value = self.followup(context)
        malformed = {
            **value,
            "command": {**value["command"], "issue_number": "private-github"},
        }
        rejected = self.client.post(
            path,
            json={"lease_token": claim["lease_token"], "request": malformed},
            headers=headers,
        )
        self.assertEqual(rejected.status_code, 422)
        self.assertNotIn("private-github", rejected.text)

    def test_followup_trace_filters_granted_credentials_before_persistence(self):
        from app.project_context import decrypt

        scope, _, _, claim, headers, context = self.builtin_claim()
        path = (
            "/api/v1/shared-worker/assignments/" + claim["assignment_id"] + "/followups"
        )
        value = self.followup(context)
        traced = {
            **value,
            "command": {
                **value["command"],
                "trace_context": {"baggage": "private-github"},
            },
        }
        accepted = self.client.post(
            path,
            json={"lease_token": claim["lease_token"], "request": traced},
            headers=headers,
        )
        self.assertEqual(accepted.status_code, 202, accepted.text)
        with self.sessions() as db:
            document = decrypt(
                db.get(
                    models.ProjectFollowup, accepted.json()["followup_id"]
                ).ciphertext,
                self.settings,
            )
            self.assertNotIn("private-github", str(document))

    def test_echo_start_budget_is_fenced_and_uses_pinned_stage(self):
        scope, _, definition = self.arrange()
        _, claim, _ = self.claim_run(scope, definition)
        from app.worker_pools import assignment_deadline

        with self.sessions() as db:
            row = db.get(models.Assignment, claim["assignment_id"])
            self.assertIsNotNone(row.started_at)
            self.assertEqual(
                assignment_deadline(db, row, self.settings) - row.started_at, 300
            )

    def test_run_detail_exposes_pinned_dag_context_and_owned_attempt_worker_links(self):
        scope, repo, definition = self.arrange()
        response = self.launch(scope, definition)
        run_id = response.json()["id"]
        detail = self.client.get(
            f"/api/v1/projects/{scope.project_id}/workflow-runs/{run_id}"
        ).json()
        self.assertEqual(detail["definition_id"], definition["id"])
        stage = detail["stages"][0]
        self.assertEqual(stage["id"], "echo-0")
        self.assertEqual(stage["executor"], "builtin@1")
        self.assertEqual(stage["repository_id"], repo)
        self.assertEqual(stage["settings"]["openai_model"], "model-before")
        self.assertNotIn("secret_versions", stage)
        self.assertNotIn("private input", str(detail))
        attempt = detail["attempts"][0]
        self.assertIsNone(attempt["worker_id"])
        self.assertIsNone(attempt["job_id"])
        self.assertEqual(stage["after"], [])
        self.assertEqual(detail["default_repository"], "backend")

    def test_operator_job_detail_distinguishes_cancellation_intent_from_terminal_receipt(
        self,
    ):
        import test_docker_jobs as jobs

        case = jobs.JobBrokerTests("test_same_key_durable_reattach_and_digest_conflict")
        case.__dict__.update(self.__dict__)
        case.arrange()
        receipt = case.submit()
        case.broker.cancel(case.scope, receipt.job_id)
        response = self.client.get(
            f"/api/v1/projects/{case.scope.project_id}/jobs/{receipt.job_id}"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["cancel_requested"])
        self.assertNotEqual(response.json()["state"], "cancelled")
        detail = self.client.get(
            f"/api/v1/projects/{case.scope.project_id}/workflow-runs/{case.run.id}"
        ).json()
        self.assertEqual(detail["attempts"][0]["job_id"], receipt.job_id)
        self.assertIsNotNone(detail["attempts"][0]["worker_id"])


class DynamicMigrationTests(unittest.TestCase):
    def test_populated_upgrade_backfills_started_authority_and_roundtrips(self):
        import os
        import tempfile
        import uuid
        from unittest.mock import patch
        from sqlalchemy import create_engine, inspect, text
        from sqlalchemy.engine import make_url
        from alembic.config import Config
        from alembic import command

        with tempfile.TemporaryDirectory() as tmp:
            url = os.environ.get(
                "CONTROL_MIGRATION_TEST_URL", "sqlite:///" + tmp + "/runtime.db"
            )
            admin = None
            if url.startswith("postgresql"):
                admin = create_engine(url)
                schema = "dynamic_migration_" + uuid.uuid4().hex
                with admin.begin() as db:
                    db.execute(text('CREATE SCHEMA "' + schema + '"'))
                url = (
                    make_url(url)
                    .update_query_dict({"options": "-csearch_path=" + schema})
                    .render_as_string(hide_password=False)
                )
            engine = None
            try:
                cfg = Config(str(fixtures.ROOT / "control-plane/backend/alembic.ini"))
                with patch.dict(os.environ, {"CONTROL_DATABASE_URL": url}):
                    command.upgrade(cfg, "e201f1000007")
                    engine = create_engine(url)
                    with engine.begin() as db:
                        db.execute(
                            models.RepositoryConnection.__table__.insert().values(
                                id="repo",
                                project_id=models.DEFAULT_PROJECT_ID,
                                alias="repo",
                                owner="owner",
                                name="repo",
                            )
                        )
                        db.execute(
                            models.WorkerPool.__table__.insert().values(
                                id="pool", name="pool", profile={}
                            )
                        )
                        db.execute(
                            text(
                                "INSERT INTO execution_assignments(id,project_id,repository_id,pool_id,run_id,stage_id,execution_id,role,executor,action,status,input_ciphertext,created_at) VALUES ('legacy',:project,'repo','pool','run','load','execution','simple','builtin@1','issue.load@1','running','encrypted',5)"
                            ),
                            {"project": models.DEFAULT_PROJECT_ID},
                        )
                    command.upgrade(cfg, "head")
                    self.assertIn(
                        "dynamic_dispatches", inspect(engine).get_table_names()
                    )
                    self.assertIn(
                        "project_followups", inspect(engine).get_table_names()
                    )
                    with engine.connect() as db:
                        self.assertEqual(
                            db.execute(
                                text(
                                    "SELECT started_at FROM execution_assignments WHERE id='legacy'"
                                )
                            ).scalar(),
                            5,
                        )
                    command.downgrade(cfg, "e201f1000007")
                    self.assertNotIn(
                        "project_followups", inspect(engine).get_table_names()
                    )
                    self.assertNotIn(
                        "started_at",
                        [
                            column["name"]
                            for column in inspect(engine).get_columns(
                                "execution_assignments"
                            )
                        ],
                    )
                    command.upgrade(cfg, "head")
            finally:
                if engine:
                    engine.dispose()
                if admin:
                    with admin.begin() as db:
                        db.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
                    admin.dispose()
