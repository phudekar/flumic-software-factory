"""Invocation-local secret filtering; no shared registry or environment mutation."""

from contextlib import contextmanager
from contextvars import ContextVar

_secrets = ContextVar("factory_action_secrets", default=())


@contextmanager
def secret_scope(values):
    token = _secrets.set(
        tuple(sorted(set(filter(None, values)), key=len, reverse=True))
    )
    try:
        yield
    finally:
        _secrets.reset(token)


def redact(value):
    if isinstance(value, str):
        for secret in _secrets.get():
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {redact(str(key)): redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value
