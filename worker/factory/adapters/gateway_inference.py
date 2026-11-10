"""OpenAI-compatible gateway with an operator-owned route and no automatic retry."""
from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from time import monotonic
from types import MappingProxyType
from urllib.parse import urlsplit

import httpx
import tiktoken

from factory.adapters.token_budget import count_request_tokens
from factory.application.model_sessions import (
    ModelRequest,
    ModelResponse,
    ModelTransportError,
    TokenAssessment,
    chat_response_format,
)
from factory.application.telemetry import ModelUsage


@dataclass(frozen=True)
class LLMConnectionRevision:
    revision_id: str
    endpoint: str
    credential_ref: str
    aliases: Mapping[str, str]
    timeout_seconds: float = 90.0

    def __post_init__(self) -> None:
        url = urlsplit(self.endpoint)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.path != "/v1"
            or url.query
            or url.fragment
            or any(char.isspace() for char in self.endpoint)
            or "?" in self.endpoint
            or "#" in self.endpoint
            or "\\" in self.endpoint
            or url.port == 0
        ):
            raise ValueError("Gateway endpoint must be an approved HTTPS URL ending in /v1")
        if not self.revision_id or not self.credential_ref or not self.aliases:
            raise ValueError("Gateway revision, credential reference and model aliases are required")
        if not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 300:
            raise ValueError("Gateway timeout must be between 0 and 300 seconds")
        aliases = dict(self.aliases)
        for alias, tokenizer_model in aliases.items():
            if not isinstance(alias, str) or not alias or not isinstance(tokenizer_model, str):
                raise ValueError("Gateway alias requires a qualified tokenizer")
            try:
                tiktoken.encoding_name_for_model(tokenizer_model)
            except KeyError:
                raise ValueError("Gateway alias requires a qualified tokenizer") from None
        object.__setattr__(self, "aliases", MappingProxyType(aliases))


def assess_gateway_tokens(connection: LLMConnectionRevision, request: ModelRequest) -> TokenAssessment:
    if (
        type(request.max_completion_tokens) is not int
        or type(request.max_context_tokens) is not int
        or not 0 < request.max_completion_tokens <= request.max_context_tokens
    ):
        raise ValueError("Gateway token limits must be positive and within the context budget")
    tokenizer = connection.aliases.get(request.model)
    if tokenizer is None:
        raise ValueError("Model alias has no qualified tokenizer")
    chat_response_format(request)
    count = count_request_tokens(
        request.messages,
        tokenizer,
        max_context_tokens=request.max_context_tokens,
        max_completion_tokens=request.max_completion_tokens,
    )
    return TokenAssessment(count, "qualified_local_tokenizer")



class GatewayTransport:
    provider = "gateway"

    def __init__(
        self, connection: LLMConnectionRevision, token: str, *, client: httpx.Client | None = None
    ) -> None:
        if not token:
            raise ValueError("Gateway inference credential is required")
        self._connection = connection
        self._token = token
        self._client = client

    def assess_tokens(self, request: ModelRequest) -> TokenAssessment:
        return assess_gateway_tokens(self._connection, request)

    def infer(self, request: ModelRequest) -> ModelResponse:
        self.assess_tokens(request)
        try:
            if self._client is None:
                with httpx.Client(trust_env=False) as client:
                    response = self._post(client, request)
            else:
                response = self._post(self._client, request)
        except httpx.RequestError:
            raise ModelTransportError("Gateway request outcome is uncertain", uncertain=True) from None
        try:
            return _read_response(json.loads(response))
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            raise ModelTransportError("Gateway returned an invalid JSON completion", uncertain=True) from None

    def _post(self, client: httpx.Client, request: ModelRequest) -> bytes:
        deadline = monotonic() + self._connection.timeout_seconds
        with client.stream(
            "POST",
            self._connection.endpoint + "/chat/completions",
            json={
                "model": request.model,
                "messages": request.messages,
                "response_format": chat_response_format(request),
                "max_completion_tokens": request.max_completion_tokens,
            },
            headers={
                "Authorization": "Bearer " + self._token,
                "X-Request-ID": request.request_id,
                "Accept-Encoding": "identity",
            },
            timeout=self._connection.timeout_seconds,
            follow_redirects=False,
        ) as response:
            if not response.is_success:
                raise ModelTransportError(
                    f"Gateway request failed (HTTP {response.status_code})",
                    uncertain=response.status_code >= 500 or response.status_code == 408,
                    status_code=response.status_code,
                )
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise ModelTransportError("Unexpected gateway response encoding", uncertain=True)
            body = bytearray()
            for chunk in response.iter_raw():
                if monotonic() >= deadline:
                    raise ModelTransportError("Gateway request outcome is uncertain", uncertain=True)
                if len(body) + len(chunk) > 4 * 1024 * 1024:
                    raise ModelTransportError(
                        "Gateway completion exceeds the response size limit", uncertain=True
                    )
                body.extend(chunk)
            return bytes(body)


def _read_response(body: dict) -> ModelResponse:
    choices = body["choices"]
    choice = choices[0]
    content = choice["message"]["content"]
    if choice.get("finish_reason") != "stop" or not isinstance(json.loads(content), dict):
        raise ValueError("Expected a complete JSON object")
    model = body["model"]
    response_id = body["id"]
    if not isinstance(model, str) or not isinstance(response_id, str) or not model or not response_id:
        raise ValueError("Expected model and response identity")
    usage = body.get("usage")
    recorded_usage = None
    if usage is not None:
        def count(source, name):
            value = source.get(name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Invalid token count")
            return value

        input_tokens = count(usage, "prompt_tokens")
        output_tokens = count(usage, "completion_tokens")
        if input_tokens is None or output_tokens is None:
            raise ValueError("Missing token count")
        recorded_usage = ModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=count(usage.get("prompt_tokens_details") or {}, "cached_tokens"),
            reasoning_tokens=count(usage.get("completion_tokens_details") or {}, "reasoning_tokens"),
            response_model=model,
        )
    return ModelResponse(
        content=content,
        usage=recorded_usage,
        usage_source="gateway_reported" if recorded_usage else None,
        response_model=model,
        response_id=response_id,
        choice_count=len(choices),
        finish_reason="stop",
    )
