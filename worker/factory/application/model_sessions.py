"""Model inference boundary shared by stage clients and transport adapters."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from factory.application.telemetry import ModelUsage
from factory.domain.models import ModelError


@dataclass(frozen=True)
class ModelRequest:
    model: str
    messages: list[dict[str, str]]
    max_completion_tokens: int
    max_context_tokens: int
    request_id: str
    protocol: Literal["chat_completions"] = "chat_completions"
    json_schema: dict | None = None


@dataclass(frozen=True)
class TokenAssessment:
    input_tokens: int
    source: Literal["local_tokenizer", "provider_reported", "qualified_local_tokenizer"]


@dataclass(frozen=True)
class ModelResponse:
    content: str | None
    usage: ModelUsage | None
    usage_source: Literal["provider_reported", "gateway_reported"] | None
    response_model: str | None = None
    response_id: str | None = None
    total_tokens: int | None = None
    choice_count: int = 0
    finish_reason: str | None = None
    refused: bool = False


class ModelTransportError(ModelError):
    def __init__(self, message: str, *, uncertain: bool = False) -> None:
        super().__init__(message)
        self.uncertain = uncertain


class ModelTransport(Protocol):
    provider: str

    def assess_tokens(self, request: ModelRequest) -> TokenAssessment: ...

    def infer(self, request: ModelRequest) -> ModelResponse: ...


def chat_response_format(request: ModelRequest) -> dict:
    if request.protocol != "chat_completions":
        raise ModelTransportError("Unsupported model protocol")
    if request.json_schema is not None:
        raise ModelTransportError("Strict JSON schema transport is not qualified")
    return {"type": "json_object"}
