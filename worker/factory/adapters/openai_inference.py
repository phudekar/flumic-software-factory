"""OpenAI SDK adapter preserving the existing Direct inference behavior."""
from __future__ import annotations

from factory.adapters.token_budget import count_request_tokens
from factory.application.model_sessions import (
    ModelRequest,
    ModelResponse,
    ModelTransportError,
    TokenAssessment,
    chat_response_format,
)
from factory.application.telemetry import ModelUsage


class OpenAITransport:
    provider = "openai"

    def __init__(self, client) -> None:
        self._client = client

    def assess_tokens(self, request: ModelRequest) -> TokenAssessment:
        chat_response_format(request)
        used_provider = False

        def remote_count(model, messages):
            nonlocal used_provider
            used_provider = True
            return self._client.responses.input_tokens.count(model=model, input=messages).input_tokens

        count = count_request_tokens(
            request.messages,
            request.model,
            max_context_tokens=request.max_context_tokens,
            max_completion_tokens=request.max_completion_tokens,
            remote_counter=remote_count,
        )
        return TokenAssessment(count, "provider_reported" if used_provider else "local_tokenizer")

    def infer(self, request: ModelRequest) -> ModelResponse:
        response_format = chat_response_format(request)
        try:
            response = self._client.chat.completions.create(
                model=request.model,
                messages=request.messages,
                response_format=response_format,
                max_completion_tokens=request.max_completion_tokens,
            )
        except Exception as error:
            details = [type(error).__name__]
            status = getattr(error, "status_code", None)
            if type(status) is int:
                details.append(f"HTTP {status}")
            raise ModelTransportError(
                "Model request failed (" + ", ".join(details) + ")"
            ) from None
        usage = getattr(response, "usage", None)
        choice = response.choices[0] if response.choices else None
        message = choice.message if choice else None
        return ModelResponse(
            content=message.content if message else None,
            usage=ModelUsage(
                input_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
                cached_input_tokens=getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", None),
                reasoning_tokens=getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None),
                response_model=response.model,
            ) if usage is not None else None,
            usage_source="provider_reported" if usage is not None else None,
            response_model=getattr(response, "model", None),
            response_id=getattr(response, "id", None),
            total_tokens=getattr(usage, "total_tokens", None),
            choice_count=len(response.choices),
            finish_reason=getattr(choice, "finish_reason", None),
            refused=bool(message and getattr(message, "refusal", None)),
        )
