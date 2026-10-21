"""Read usage metadata without retaining or exporting CLI event contents."""

import json

from factory.application.telemetry import ModelUsage, model_call, record_usage


def _count(value):
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


def _record_codex_usage(stdout):
    totals = {}
    found = False
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        found = True
        for key in (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "reasoning_output_tokens",
        ):
            value = _count(usage.get(key))
            if value is not None:
                totals[key] = totals.get(key, 0) + value
    if found:
        record_usage(
            ModelUsage(
                input_tokens=totals.get("input_tokens"),
                output_tokens=totals.get("output_tokens"),
                cached_input_tokens=totals.get("cached_input_tokens"),
                reasoning_tokens=totals.get("reasoning_output_tokens"),
            )
        )


def _read_claude_result(stdout):
    try:
        result = json.loads(stdout)
    except ValueError:
        # Compatibility with injected/older runners; usage remains unknown.
        return stdout
    if not isinstance(result, dict) or "result" not in result:
        return stdout
    if result.get("is_error"):
        from factory.domain.models import ModelError

        raise ModelError("Claude Code reported an unsuccessful result")
    models = result.get("modelUsage")
    if isinstance(models, dict):
        for model, usage in models.items():
            if not isinstance(usage, dict):
                continue
            direct_input = _count(usage.get("inputTokens"))
            cached = _count(usage.get("cacheReadInputTokens"))
            # Normalize to inclusive input, so cached input is not billed twice.
            inclusive_input = (
                direct_input + cached
                if direct_input is not None and cached is not None
                else None
            )
            with model_call("anthropic", model, agent="claude.model") as call:
                call.record(
                    ModelUsage(
                        input_tokens=inclusive_input,
                        output_tokens=_count(usage.get("outputTokens")),
                        cached_input_tokens=cached,
                        cache_creation_tokens=_count(
                            usage.get("cacheCreationInputTokens")
                        ),
                        response_model=model,
                        reported_cost_usd=usage.get("costUSD")
                        if isinstance(usage.get("costUSD"), (int, float))
                        else None,
                    )
                )
    text = result.get("result")
    return text if isinstance(text, str) else stdout


def record_codex_usage(stdout):
    try:
        _record_codex_usage(stdout)
    except (ValueError, TypeError):
        # Malformed accounting is unknown, never substituted with zero.
        pass


def read_claude_result(stdout):
    try:
        return _read_claude_result(stdout)
    except (ValueError, TypeError):
        try:
            result = json.loads(stdout)
            text = result.get("result") if isinstance(result, dict) else None
            return text if isinstance(text, str) else stdout
        except ValueError:
            return stdout
