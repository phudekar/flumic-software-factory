from __future__ import annotations

import tiktoken

MAX_CONTEXT_TOKENS = 32_000
MAX_COMPLETION_TOKENS = 16_000
MODEL_NAME = "gpt-4.1-mini"


def count_request_tokens(
    messages: list[dict[str, str]],
    model: str = MODEL_NAME,
    *,
    max_context_tokens: int = MAX_CONTEXT_TOKENS,
    max_completion_tokens: int = MAX_COMPLETION_TOKENS,
) -> int:
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError as error:
        raise ValueError(
            "Configured model has no known tokenizer; refusing outbound request"
        ) from error
    token_count = 16 * len(messages) + 2
    for message in messages:
        token_count += len(encoding.encode(message["role"]))
        token_count += len(encoding.encode(message["content"]))
    if token_count + max_completion_tokens > max_context_tokens:
        raise ValueError(
            f"Model request exceeds the configured {max_context_tokens:,}-token total budget"
        )
    return token_count
