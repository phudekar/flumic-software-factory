import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))

from factory.adapters.gateway_inference import LLMConnectionRevision, GatewayTransport
from factory.application.model_sessions import ModelRequest, ModelTransportError


class GatewayTransportTests(unittest.TestCase):
    def setUp(self):
        self.request = ModelRequest("planning", [{"role": "user", "content": "Return JSON"}], 100, 2000, "request-1")
        self.calls = []

    def transport(self, handler):
        def record(request):
            self.calls.append(request)
            reply = handler(request)
            return httpx.Response(
                reply.status_code, headers=reply.headers, stream=httpx.ByteStream(reply.content)
            )

        connection = LLMConnectionRevision(
            revision_id="rev-1",
            endpoint="https://gateway.example/v1",
            credential_ref="gateway-token",
            aliases={"planning": "gpt-4.1-mini"},
        )
        client = httpx.Client(transport=httpx.MockTransport(record))
        self.addCleanup(client.close)
        return GatewayTransport(connection, "test-token", client=client)

    def test_approved_alias_enforces_budget_and_preserves_json_contract(self):
        transport = self.transport(lambda _: httpx.Response(200, json={
            "id": "completion-1",
            "model": "gpt-4.1-mini",
            "choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 25, "completion_tokens": 7},
        }))
        self.assertEqual(transport.assess_tokens(self.request).source, "qualified_local_tokenizer")
        response = transport.infer(self.request)
        self.assertEqual(response.content, '{"ok":true}')
        self.assertEqual(response.usage.input_tokens, 25)
        self.assertEqual(response.usage_source, "gateway_reported")
        sent, = self.calls
        self.assertEqual(str(sent.url), "https://gateway.example/v1/chat/completions")
        self.assertEqual(sent.headers["authorization"], "Bearer test-token")
        self.assertEqual(sent.headers["x-request-id"], "request-1")
        self.assertEqual(sent.headers["accept-encoding"], "identity")
        body = json.loads(sent.content)
        self.assertEqual(body["model"], "planning")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["max_completion_tokens"], 100)

    def test_unknown_alias_denied_before_network_io(self):
        transport = self.transport(lambda _: self.fail("unexpected network call"))
        request = ModelRequest("gpt-4.1-mini", [], 100, 2000, "request-2")
        with self.assertRaisesRegex(ValueError, "alias"):
            transport.infer(request)
        self.assertEqual(self.calls, [])

    def test_invalid_json_and_redirect_never_retry_or_fallback(self):
        for reply, uncertain in (
            (httpx.Response(200, json={"choices": [{"message": {"content": "not JSON"}}]}), True),
            (httpx.Response(200, content=b"x" * (4 * 1024 * 1024 + 1)), True),
            (httpx.Response(200, headers={"Content-Encoding": "gzip"}), True),
            (httpx.Response(504), True),
            (httpx.Response(307, headers={"Location": "https://other.example"}), False),
        ):
            with self.subTest(status=reply.status_code):
                self.calls.clear()
                transport = self.transport(lambda _: reply)
                with self.assertRaises(ModelTransportError) as captured:
                    transport.infer(self.request)
                self.assertEqual(captured.exception.uncertain, uncertain)
                self.assertEqual(len(self.calls), 1)

    def test_uncertain_timeout_is_not_retried(self):
        def timeout(_):
            raise httpx.ReadTimeout("sensitive upstream body")

        transport = self.transport(timeout)
        with self.assertRaises(ModelTransportError) as captured:
            transport.infer(self.request)
        self.assertTrue(captured.exception.uncertain)
        self.assertNotIn("sensitive", str(captured.exception))
        self.assertEqual(len(self.calls), 1)

    def test_unapproved_endpoint_shapes_fail_validation(self):
        for endpoint in (
            "http://gateway.example/v1", "https://user:secret@gateway.example/v1",
            "https://gateway.example/v1?key=secret", "https://gateway.example/elsewhere",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                LLMConnectionRevision("rev-1", endpoint, "gateway-token", {"planning": "gpt-4.1-mini"})

    def test_unsupported_schema_and_invalid_budget_fail_before_request(self):
        transport = self.transport(lambda _: self.fail("unexpected network call"))
        with self.assertRaisesRegex(ModelTransportError, "schema"):
            transport.infer(replace(self.request, json_schema={"type": "object"}))
        with self.assertRaisesRegex(ValueError, "positive"):
            transport.infer(replace(self.request, max_completion_tokens=-1))
        self.assertEqual(self.calls, [])
