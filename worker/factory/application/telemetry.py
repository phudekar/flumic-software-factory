"""Provider-neutral usage recording. No prompts or source content enter this API."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int | None = (
        None  # Includes cached input; excludes cache creation for Anthropic.
    )
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_creation_tokens: int | None = None
    reasoning_tokens: int | None = None  # Subset of output, never added twice.
    reported_cost_usd: float | None = None
    response_model: str | None = None

    def __post_init__(self):
        import math

        for name in (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "cache_creation_tokens",
            "reasoning_tokens",
        ):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError("Token usage must be a nonnegative integer or unknown")
        if (
            self.cached_input_tokens is not None
            and self.input_tokens is not None
            and self.cached_input_tokens > self.input_tokens
        ):
            raise ValueError("Cached tokens cannot exceed inclusive input tokens")
        if self.reported_cost_usd is not None and (
            not math.isfinite(self.reported_cost_usd) or self.reported_cost_usd < 0
        ):
            raise ValueError("Reported cost must be nonnegative and finite")


class UsageRecorder(Protocol):
    def record(self, usage: ModelUsage) -> None: ...


class NullCall:
    def record(self, usage):
        pass


_current_call = ContextVar("factory_current_model_call", default=None)


def record_usage(usage):
    call = _current_call.get()
    if call is not None:
        call.record(usage)


_recorder = ContextVar("factory_usage_recorder", default=None)
_incoming = ContextVar("factory_incoming_trace", default={})


@contextmanager
def use_recorder(recorder):
    token = _recorder.set(recorder)
    try:
        yield
    finally:
        _recorder.reset(token)


@contextmanager
def incoming_trace(carrier):
    token = _incoming.set(carrier or {})
    try:
        yield
    finally:
        _incoming.reset(token)


def incoming_carrier():
    return dict(_incoming.get())


def capture_trace():
    recorder = _recorder.get()
    return recorder.capture_trace() if recorder else {}


@contextmanager
def model_call(
    provider: str,
    model: str | None,
    *,
    agent: str = "api",
    iteration: int | None = None,
):
    recorder = _recorder.get()
    if recorder is None:
        yield NullCall()
    else:
        with recorder.model_call(
            provider, model, agent=agent, iteration=iteration
        ) as call:
            token = _current_call.set(call)
            try:
                yield call
            finally:
                _current_call.reset(token)


def current_recorder():
    return _recorder.get()


def record_conversation(*, request=None, response=None, coverage="api_exchange"):
    call = _current_call.get()
    capture = getattr(call, "conversation", None)
    if capture is not None:
        capture(request=request, response=response, coverage=coverage)
