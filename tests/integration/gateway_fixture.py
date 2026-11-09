"""Disposable LiteLLM protocol fixture. Run through scripts/test-gateway-contracts.sh."""
import json
import ssl
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


class FakeUpstream(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if (
            self.path != "/v1/chat/completions"
            or self.headers.get("Authorization") != "Bearer sk-fake-upstream"
            or body.get("model") != "gpt-4.1-mini"
            or body.get("response_format") != {"type": "json_object"}
            or body.get("max_completion_tokens") != 100
            or body.get("messages") != [{"role": "user", "content": "Return fixture JSON"}]
        ):
            self.send_error(400, "Unexpected upstream request")
            return
        reply = json.dumps({
            "id": "chatcmpl-fixture",
            "object": "chat.completion",
            "created": 1,
            "model": "gpt-4.1-mini",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": '{"fixture":true}'},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 18, "completion_tokens": 5, "total_tokens": 23},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)


def probe():
    import httpx

    from factory.adapters.gateway_inference import LLMConnectionRevision, GatewayTransport
    from factory.application.model_sessions import ModelRequest, ModelTransportError

    context = ssl.create_default_context(cafile="/fixture/server.crt")
    origin = "https://127.0.0.1:4000"
    connection = LLMConnectionRevision(
        revision_id="fixture-v1",
        endpoint=origin + "/v1",
        credential_ref="fixture-only",
        aliases={"planning": "gpt-4.1-mini"},
        timeout_seconds=30,
    )
    request = ModelRequest(
        model="planning",
        messages=[{"role": "user", "content": "Return fixture JSON"}],
        max_completion_tokens=100,
        max_context_tokens=2000,
        request_id="flumic-fixture-1",
    )
    with httpx.Client(verify=context, trust_env=False, timeout=2) as client:
        for _ in range(45):
            try:
                if client.get(origin + "/health/liveliness").is_success:
                    break
            except httpx.RequestError:
                pass
            time.sleep(1)
        else:
            raise RuntimeError("LiteLLM did not become ready")
        try:
            GatewayTransport(connection, "sk-invalid-fixture", client=client).infer(request)
        except ModelTransportError as error:
            # A non-master key requires a database in this pinned OSS fixture.
            assert "HTTP 400" in str(error), str(error)
        else:
            raise AssertionError("Gateway accepted an invalid credential")

        transport = GatewayTransport(connection, "sk-flumic-fixture", client=client)
        assessment = transport.assess_tokens(request)
        response = transport.infer(request)
        assert assessment.source == "qualified_local_tokenizer"
        assert json.loads(response.content) == {"fixture": True}
        assert response.usage_source == "gateway_reported"
        assert response.usage.input_tokens == 18 and response.usage.output_tokens == 5
        print(json.dumps({
            "gateway": "LiteLLM OSS v1.106.6",
            "protocol": request.protocol,
            "tls": "fixture CA verified",
            "json_object": True,
            "completion_limit_forwarded": True,
            "invalid_credential_denied": True,
            "count_source": assessment.source,
            "usage_source": response.usage_source,
        }, sort_keys=True))


if __name__ == "__main__":
    if sys.argv[1:] == ["upstream"]:
        HTTPServer(("0.0.0.0", 8080), FakeUpstream).serve_forever()
    elif sys.argv[1:] == ["probe"]:
        probe()
    else:
        raise SystemExit("Expected upstream or probe")
