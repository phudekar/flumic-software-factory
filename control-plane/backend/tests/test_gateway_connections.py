"""Administrator-owned gateway revision and credential boundaries."""
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_api as fixtures
import test_workflow_state as workflow_fixtures
from app.database import SecretVersion
from app.ports import ConnectionCapabilities


class GatewayConnectionTests(unittest.TestCase):
    setUp = fixtures.ControlPlaneTests.setUp
    tearDown = fixtures.ControlPlaneTests.tearDown
    setup_graph = workflow_fixtures.WorkflowStateTests.setup_graph
    publish = workflow_fixtures.WorkflowStateTests.publish
    worker = workflow_fixtures.WorkflowStateTests.worker
    arrange = workflow_fixtures.WorkflowStateTests.arrange

    def arrange_gateway(self):
        scope, _, _ = self.arrange()
        self.project_id = scope.project_id
        self.path = f"/api/v1/projects/{self.project_id}/gateway-connections"
        self.inference = self.credential("gateway-inference")
        self.admin = self.credential("gateway-management")
        return {
            "endpoint": "https://gateway.example/v1",
            "inference_credential_id": self.inference,
            "management_credential_id": self.admin,
            "aliases": {"planner": "gpt-4o-mini"},
        }

    def credential(self, purpose, *, project=None, openbao_version=1):
        identifier = str(uuid4())
        with self.sessions.begin() as db:
            db.add(SecretVersion(id=identifier, project_id=project or self.project_id,
                                 purpose=purpose, name=purpose, openbao_version=openbao_version))
        return identifier

    def test_admin_creates_immutable_revision_without_reading_secrets(self):
        payload = self.arrange_gateway()
        response = self.client.post(self.path, json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        first = response.json()
        self.assertEqual(first["aliases"], payload["aliases"])
        self.assertEqual(first["inference_credential_id"], self.inference)
        self.assertEqual(first["management_credential_id"], self.admin)
        self.assertNotIn("openbao_version", first)
        second = self.client.post(self.path, json={
            **payload, "parent_id": first["id"], "aliases": {"reviewer": "gpt-4o"},
        })
        self.assertEqual(second.status_code, 201, second.text)
        self.assertNotEqual(first["id"], second.json()["id"])
        self.assertEqual(self.client.get(self.path + "/" + first["id"]).json(), first)

    def test_cross_project_and_wrong_credential_purpose_are_denied(self):
        payload = self.arrange_gateway()
        other, _, _ = self.arrange("other")
        foreign = self.credential("gateway-inference", project=other.project_id)
        for key, credential, status in (
            ("inference_credential_id", foreign, 404),
            ("inference_credential_id", self.admin, 409),
            ("management_credential_id", self.inference, 409),
            ("inference_credential_id", self.credential("gateway-inference", openbao_version=None), 409),
        ):
            with self.subTest(key=key, credential=credential):
                response = self.client.post(self.path, json={**payload, key: credential})
                self.assertEqual(response.status_code, status, response.text)

    def test_session_and_csrf_are_required(self):
        payload = self.arrange_gateway()
        response = self.client.post(self.path, json=payload, headers={"X-CSRF-Token": ""})
        self.assertEqual(response.status_code, 403)
        self.client.cookies.clear()
        response = self.client.post(self.path, json=payload)
        self.assertEqual(response.status_code, 401)

    def test_verification_uses_only_management_credential(self):
        payload = self.arrange_gateway()
        created = self.client.post(self.path, json=payload)
        self.assertEqual(created.status_code, 201)
        path = self.path + "/" + created.json()["id"] + "/verify"
        with (
            patch("app.gateway_connections.read_credential", return_value="private-management-key") as read,
            patch("app.adapters.litellm_management.LiteLLMAdministration.verify",
                  return_value=ConnectionCapabilities(("planner",), ())) as verify,
        ):
            response = self.client.post(path)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), {"available_aliases": ["planner"], "missing_aliases": []})
            self.assertEqual(read.call_args.args[0].id, self.admin)
            self.assertEqual(verify.call_args.args[2], "private-management-key")
            self.assertNotIn("private-management-key", response.text)
            self.assertEqual(self.client.post(path, headers={"X-CSRF-Token": ""}).status_code, 403)
            verify.assert_called_once()


class LiteLLMDiscoveryTests(unittest.TestCase):
    def test_catalog_projects_only_approved_aliases_and_rejects_redirects(self):
        import httpx
        from app.adapters.litellm_management import LiteLLMAdministration
        from app.ports import GatewayVerificationError

        calls = []

        def handler(request):
            calls.append(str(request.url))
            self.assertEqual(request.headers["authorization"], "Bearer management-key")
            if len(calls) == 2:
                return httpx.Response(302, headers={"Location": "https://other.example/catalog"})
            return httpx.Response(200, stream=httpx.ByteStream(
                b'{"data":[{"model_name":"planner","litellm_params":{"api_key":"private"}},'
                b'{"model_name":"other"}]}'
            ))

        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        real_client = httpx.Client
        with patch("app.adapters.litellm_management.httpx.Client", side_effect=client):
            adapter = LiteLLMAdministration()
            result = adapter.verify("https://gateway.example/v1", ("planner", "missing"), "management-key")
            self.assertEqual(result, ConnectionCapabilities(("planner",), ("missing",)))
            with self.assertRaises(GatewayVerificationError):
                adapter.verify("https://gateway.example/v1", ("planner",), "management-key")
        self.assertEqual(calls, ["https://gateway.example/v1/model/info"] * 2)
