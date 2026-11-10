"""The inference facade derives authority from the pinned run and its bearer."""
import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import select

from app import database as models
from app.project_context import decrypt, encrypt
from factory.application.model_sessions import ModelResponse, ModelTransportError
from factory.application.telemetry import ModelUsage
import test_broker_capabilities as capabilities
import test_gateway_connections as gateways


class ModelBrokerTests(unittest.TestCase):
    setUp = capabilities.BrokerCapabilitiesTests.setUp
    tearDown = capabilities.BrokerCapabilitiesTests.tearDown
    setup_graph = capabilities.BrokerCapabilitiesTests.setup_graph
    publish = capabilities.BrokerCapabilitiesTests.publish
    worker = capabilities.BrokerCapabilitiesTests.worker
    arrange = capabilities.BrokerCapabilitiesTests.arrange
    start = capabilities.BrokerCapabilitiesTests.start
    assignment = capabilities.BrokerCapabilitiesTests.assignment
    issue = capabilities.BrokerCapabilitiesTests.issue
    credential = gateways.GatewayConnectionTests.credential

    def gateway_run(self):
        project, scope = self.assignment()
        self.project_id = project.project_id
        credential = self.credential("gateway-inference")
        response = self.client.post(f"/api/v1/projects/{self.project_id}/gateway-connections", json={
            "endpoint": "https://gateway.example/v1",
            "inference_credential_id": credential,
            "aliases": {"planner": "gpt-4o-mini"},
        })
        self.assertEqual(response.status_code, 201, response.text)
        with self.sessions.begin() as db:
            run = db.get(models.WorkflowRun, scope.run_id)
            snapshot = db.get(models.WorkflowLaunchSnapshot, run.snapshot_id)
            pin = decrypt(snapshot.ciphertext, self.settings)
            pin["execution_pin"]["route_revision_id"] = response.json()["id"]
            snapshot.ciphertext = encrypt(pin, self.settings)
        self.limits = self.limits.model_copy(update={"input_tokens": 100, "output_tokens": 40})
        self.bearer = self.issue(scope)
        self.body = {
            "model": "planner",
            "messages": [{"role": "user", "content": "private prompt"}],
            "max_completion_tokens": 10,
            "max_context_tokens": 100,
            "request_id": "one",
        }
        self.path = "/api/v1/model-broker/inference"
        return project, scope, credential

    def infer(self, body=None, bearer=None):
        return self.client.post(self.path, json=body or self.body,
                                headers={"Authorization": "Bearer " + (bearer or self.bearer)})

    def test_inference_is_at_most_once_and_uses_only_inference_credential(self):
        _, _, credential = self.gateway_run()
        self.client.cookies.clear()
        with (
            patch("app.model_broker.read_credential", return_value="secret") as read,
            patch("app.model_broker.GatewayTransport.infer", return_value=ModelResponse(
                content='{"answer":1}', usage=ModelUsage(input_tokens=8, output_tokens=3),
                usage_source="gateway_reported",
            )) as upstream,
        ):
            response = self.infer()
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["content"], '{"answer": 1}')
            self.assertNotIn("private prompt", response.text)
            self.assertNotIn("secret", response.text)
            self.assertEqual(self.infer().status_code, 409)
            upstream.assert_called_once()
            self.assertEqual(read.call_args.args[0].id, credential)
        with self.sessions() as db:
            row = db.scalar(select(models.BrokerReservation))
            self.assertEqual(row.state, "completed")
            self.assertEqual(row.actual_usage, {"input_tokens": 8, "output_tokens": 3})
            self.assertEqual(upstream.call_args.args[0].request_id, row.id)
            self.assertNotEqual(row.request_id, self.body["request_id"])

    def test_unapproved_alias_and_cancelled_bearer_never_resolve_secrets(self):
        project, scope, _ = self.gateway_run()
        with patch("app.model_broker.read_credential") as read:
            self.assertEqual(self.infer({**self.body, "model": "other"}).status_code, 422)
            self.store.cancel_run(project, scope.run_id)
            self.assertEqual(self.infer().status_code, 403)
            read.assert_not_called()

    def test_uncertain_outcome_is_never_redispatched(self):
        self.gateway_run()
        with (
            patch("app.model_broker.read_credential", return_value="secret"),
            patch("app.model_broker.GatewayTransport.infer",
                  side_effect=ModelTransportError("internal", uncertain=True)) as upstream,
        ):
            response = self.infer()
            self.assertEqual(response.status_code, 502, response.text)
            self.assertNotIn("internal", response.text)
            self.assertEqual(self.infer().status_code, 409)
            upstream.assert_called_once()

    def test_wrong_audience_and_oversized_body_never_read_openbao(self):
        _, scope, _ = self.gateway_run()
        other = self.issue(scope.model_copy(update={
            "audience": "repository", "operations": ("repository.read",),
        }))
        with patch("app.model_broker.read_credential") as read:
            self.assertEqual(self.infer(bearer=other).status_code, 403)
            self.assertEqual(self.infer({**self.body, "messages": [
                {"role": "user", "content": "x" * (256 * 1024)},
            ]}).status_code, 413)
            self.assertEqual(self.infer({**self.body, "max_completion_tokens": 1000}).status_code, 422)
            read.assert_not_called()

    def test_rate_limit_releases_tokens_without_retry(self):
        self.gateway_run()
        with (
            patch("app.model_broker.read_credential", return_value="secret"),
            patch("app.model_broker.GatewayTransport.infer",
                  side_effect=ModelTransportError("sensitive", status_code=429)) as upstream,
        ):
            response = self.infer()
            self.assertEqual(response.status_code, 429, response.text)
            self.assertNotIn("sensitive", response.text)
            self.assertEqual(self.infer().status_code, 409)
            upstream.assert_called_once()
        with self.sessions() as db:
            row = db.scalar(select(models.BrokerReservation))
            self.assertEqual(row.state, "rejected")

    def test_gateway_response_does_not_echo_capabilities_or_key(self):
        self.gateway_run()
        content = '{"bearer":"' + self.bearer + r'","key":"\u0070rivate-inference-key","prompt":"private prompt"}'
        with (
            patch("app.model_broker.read_credential", return_value="private-inference-key"),
            patch("app.model_broker.GatewayTransport.infer", return_value=ModelResponse(
                content=content, usage=None, usage_source=None,
            )),
        ):
            response = self.infer()
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn(self.bearer, response.text)
            self.assertNotIn("private-inference-key", response.text)
            self.assertNotIn("private prompt", response.text)
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(models.BrokerReservation)).state, "uncertain")

    def test_excess_upstream_usage_is_recorded_and_revokes_authority(self):
        self.gateway_run()
        with (
            patch("app.model_broker.read_credential", return_value="secret"),
            patch("app.model_broker.GatewayTransport.infer", return_value=ModelResponse(
                content="{}", usage=ModelUsage(input_tokens=8, output_tokens=11),
                usage_source="gateway_reported",
            )) as upstream,
        ):
            self.assertEqual(self.infer().status_code, 502)
            self.assertEqual(self.infer({**self.body, "request_id": "two"}).status_code, 403)
            upstream.assert_called_once()
        with self.sessions() as db:
            row = db.scalar(select(models.BrokerReservation))
            self.assertEqual(row.actual_usage["output_tokens"], 11)

    def test_revocation_during_openbao_read_blocks_dispatch(self):
        _, _, credential_id = self.gateway_run()

        def revoke_during_read(*_):
            with self.sessions.begin() as db:
                db.get(models.SecretVersion, credential_id).revoked_at = time.time()
            return "private-inference-key"

        with (
            patch("app.model_broker.read_credential", side_effect=revoke_during_read),
            patch("app.model_broker.GatewayTransport.infer") as upstream,
        ):
            self.assertEqual(self.infer().status_code, 409)
            upstream.assert_not_called()
        with self.sessions() as db:
            self.assertIsNone(db.scalar(select(models.BrokerReservation)))

    def test_openbao_outage_never_dispatches(self):
        self.gateway_run()
        with (
            patch("app.model_broker.read_credential", side_effect=HTTPException(503, "unavailable")),
            patch("app.model_broker.GatewayTransport.infer") as upstream,
        ):
            self.assertEqual(self.infer().status_code, 503)
            upstream.assert_not_called()


class DirectGatewayWorkerTests(unittest.TestCase):
    setUp = capabilities.BrokerCapabilitiesTests.setUp
    tearDown = capabilities.BrokerCapabilitiesTests.tearDown
    setup_graph = capabilities.BrokerCapabilitiesTests.setup_graph
    publish = capabilities.BrokerCapabilitiesTests.publish
    worker = capabilities.BrokerCapabilitiesTests.worker
    credential = gateways.GatewayConnectionTests.credential

    def worker_transport(self, endpoint):
        from app.workflow_state import WorkflowState
        from factory.adapters.broker_inference import BrokerInferenceTransport
        from factory.shared_worker import BoundTransport, SharedRequestError
        from test_builtin_bindings import BuiltinBindingTests
        import test_api as fixtures

        project, base, repos, pools, revisions = BuiltinBindingTests.setup_bundle(
            self, credentials=True,
        )
        for kind in ("issue", "feedback"):
            with self.sessions() as db:
                revision = db.get(models.WorkflowRevision, revisions[kind])
                document = decrypt(revision.graph_ciphertext, self.settings)
                definition_id = revision.definition_id
            for stage in document["stages"]:
                if stage["role"] == "developer":
                    stage["overrides"]["developer_agent"] = "api"
            updated = self.client.put(base + "/workflow-definitions/" + definition_id, json={
                "parent_id": revisions[kind], "document": document,
            })
            self.assertEqual(updated.status_code, 200, updated.text)
            revisions[kind] = updated.json()["revision_id"]
        self.project_id = project.project_id
        credential = self.credential("gateway-inference")
        route = self.client.post(base + "/gateway-connections", json={
            "endpoint": endpoint,
            "inference_credential_id": credential,
            "aliases": {"planning": "gpt-4.1-mini"},
        })
        self.assertEqual(route.status_code, 201, route.text)
        _, headers = self.worker({"id": pools["simple"]})
        registration = {
            "protocol": "shared-v1", "pool_id": pools["simple"], "role": "simple",
            "normal_slots": 2, "durable_slots": 16, "executors": ["builtin@1"],
            "actions": ["issue.load@1", "issue.plan@1"],
        }
        registered = self.client.post("/api/v1/shared-worker/register", headers=headers, json={
            **registration, "model_broker": True,
        })
        self.assertEqual(registered.status_code, 200, registered.text)
        _, old_headers = self.worker({"id": pools["simple"]}, "old-worker")
        old = self.client.post("/api/v1/shared-worker/register", headers=old_headers, json=registration)
        self.assertEqual(old.status_code, 200, old.text)
        store = WorkflowState(
            self.sessions, self.settings, fixtures.Configurations(self.settings.root),
        )
        store.enable_dynamic_repository(project, repos["backend"]["id"])
        run = self.client.post(base + "/workflow-runs", json={
            "revision_id": revisions["issue"], "input": {"issue_number": 2},
            "bindings": {repos["backend"]["id"]: revisions}, "execution_mode": "direct",
            "gateway_revision_id": route.json()["id"], "gateway_alias": "planning",
        })
        self.assertEqual(run.status_code, 202, run.text)
        run_id = run.json()["id"]
        first = store.advance_run(project, run_id).ready[0]
        store.finish_execution(project, first.execution_id,
                               output={"issue_number": 2, "state": "open", "labels": []},
                               confirmed_terminal=True)
        advance = store.advance_run(project, run_id)
        plan = advance.ready[0]
        from factory.adapters.orchestration.dynamic import action_labels

        route = advance.routes[plan.execution_id]
        self.assertEqual(
            {label.key: label.value for label in action_labels(route)}["factory_model_broker"],
            "v1",
        )
        old_claim = self.client.post("/api/v1/shared-worker/claim", headers=old_headers, json={
            **plan.model_dump(), "claim_key": "old-worker-claim-key",
        })
        self.assertEqual(old_claim.status_code, 409, old_claim.text)
        claim = self.client.post("/api/v1/shared-worker/claim", headers=headers, json={
            **plan.model_dump(), "claim_key": "test-worker-claim-key",
        })
        self.assertEqual(claim.status_code, 200, claim.text)
        claimed = claim.json()
        prefix = "/api/v1/shared-worker/assignments/" + claimed["assignment_id"]
        delivered = self.client.post(prefix + "/context", headers=headers, json={
            "lease_token": claimed["lease_token"],
        })
        self.assertEqual(delivered.status_code, 200, delivered.text)
        self.assertEqual(delivered.json()["credentials"], {"github": "private-github"})
        context = delivered.json()["context"]
        self.assertEqual(context["gateway_alias"], "planning")

        def request(path, body, *, timeout):
            response = self.client.post(path, headers=headers, json=body)
            if response.status_code != 200:
                raise SharedRequestError(response.status_code)
            return response.json()

        from types import SimpleNamespace

        bound = BoundTransport(SimpleNamespace(request=request), claimed["assignment_id"], claimed["lease_token"])
        return BrokerInferenceTransport(context["gateway_alias"], context["gateway_tokenizer"], bound.infer_model)

    def request(self):
        from factory.application.model_sessions import ModelRequest

        return ModelRequest(
            model="planning", messages=[{"role": "user", "content": "Return fixture JSON"}],
            max_context_tokens=2000, max_completion_tokens=100, request_id="worker-fixture",
        )

    def test_worker_gateway_route_uses_real_launch_and_assignment(self):
        transport = self.worker_transport("https://gateway.example/v1")
        with (
            patch("app.model_broker.read_credential", return_value="fixture-key"),
            patch("app.model_broker.GatewayTransport.infer", return_value=ModelResponse(
                content='{"fixture":true}', usage=ModelUsage(input_tokens=18, output_tokens=5),
                usage_source="gateway_reported",
            )) as upstream,
        ):
            self.assertEqual(transport.infer(self.request()).usage.output_tokens, 5)
            with self.assertRaises(ModelTransportError):
                transport.infer(self.request())
            upstream.assert_called_once()

    def test_real_local_litellm_worker_dispatch(self):
        import os
        import ssl
        import json
        import httpx
        from factory.adapters.gateway_inference import GatewayTransport

        endpoint = os.environ.get("FLUMIC_TEST_LITELLM_ENDPOINT")
        if not endpoint:
            self.skipTest("requires isolated local LiteLLM fixture")
        transport = self.worker_transport(endpoint)
        context = ssl.create_default_context(cafile=os.environ["FLUMIC_TEST_LITELLM_CA_FILE"])
        with (
            httpx.Client(verify=context, trust_env=False) as client,
            patch("app.model_broker.read_credential", return_value="sk-flumic-fixture"),
            patch("app.model_broker.GatewayTransport", side_effect=lambda connection, token: (
                GatewayTransport(connection, token, client=client)
            )),
        ):
            result = transport.infer(self.request())
            self.assertEqual(json.loads(result.content), {"fixture": True})
            self.assertEqual((result.usage.input_tokens, result.usage.output_tokens), (18, 5))
            with self.assertRaises(ModelTransportError):
                transport.infer(self.request())
