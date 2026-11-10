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
