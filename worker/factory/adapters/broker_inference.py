"""Model transport over an authenticated shared worker assignment."""
from dataclasses import asdict

from factory.adapters.token_budget import count_request_tokens
from factory.application.model_sessions import (
    ModelRequest, ModelResponse, ModelTransportError, TokenAssessment, chat_response_format,
)
from factory.application.telemetry import ModelUsage


class BrokerInferenceTransport:
    provider = "gateway"

    def __init__(self, alias, tokenizer, invoke):
        self.alias = alias
        self.tokenizer = tokenizer
        self.invoke = invoke

    def assess_tokens(self, request: ModelRequest) -> TokenAssessment:
        chat_response_format(request)
        if request.model != self.alias:
            raise ModelTransportError("Model does not match the pinned gateway alias")
        count = count_request_tokens(
            request.messages,
            self.tokenizer,
            max_context_tokens=request.max_context_tokens,
            max_completion_tokens=request.max_completion_tokens,
        )
        return TokenAssessment(count, "qualified_local_tokenizer")

    def infer(self, request: ModelRequest) -> ModelResponse:
        self.assess_tokens(request)
        body = asdict(request)
        body.pop("protocol")
        body.pop("json_schema")
        response = self.invoke(body)
        usage = response.pop("usage")
        return ModelResponse(
            **response,
            usage=ModelUsage(**usage) if usage is not None else None,
        )
