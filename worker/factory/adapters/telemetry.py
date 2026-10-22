"""Standard logging implementation of the observation port; replaceable at bootstrap."""

from __future__ import annotations

import contextvars
import json
import logging
import time
import uuid
from contextlib import contextmanager

from factory.workflows.redaction import redact
from factory.domain.models import OperationBusy
from factory.domain.safety import find_sensitive_categories

_context = contextvars.ContextVar("factory_context", default={})


class CorrelationFilter(logging.Filter):
    def filter(self, record):
        record.factory_context = {
            key: "[redacted]"
            if isinstance(value, str) and find_sensitive_categories(value)
            else value
            for key, value in redact(_context.get()).items()
        }
        message = redact(record.getMessage())
        record.msg, record.args = message, ()
        if find_sensitive_categories(message):
            record.msg, record.args = "[redacted sensitive log message]", ()
        if record.exc_info:
            record.error_type = record.exc_info[0].__name__
            record.exc_info = None
            record.exc_text = None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record):
        message = redact(record.getMessage())
        if find_sensitive_categories(message):
            message = "[redacted sensitive log message]"
        payload = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
            **{
                key: "[redacted]"
                if isinstance(value, str) and find_sensitive_categories(value)
                else value
                for key, value in getattr(record, "factory_context", {}).items()
            },
        }
        for key in ("outcome", "duration_seconds", "error_type"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        # Do not export exception repr/tracebacks, which can carry provider content.
        if record.exc_info:
            payload["error_type"] = record.exc_info[0].__name__
        return json.dumps(redact(payload), default=str)


class LoggingObserver:
    @contextmanager
    def observe(self, stage, attributes):
        context = dict(_context.get())
        context.setdefault("factory_run_id", str(uuid.uuid4()))
        context.update(attributes)
        context["stage"] = stage
        context["attempt_id"] = str(uuid.uuid4())
        token = _context.set(context)
        started = time.monotonic()
        logger = logging.getLogger("issue-worker.operations")
        logger.info("Operation started")
        outcome, error_type = "completed", None
        try:
            yield
        except OperationBusy:
            outcome = "waiting"
            raise
        except BaseException as error:
            outcome, error_type = "failed", type(error).__name__
            raise
        finally:
            logger.info(
                "Operation finished",
                extra={
                    "outcome": outcome,
                    "error_type": error_type,
                    "duration_seconds": round(time.monotonic() - started, 6),
                },
            )
            _context.reset(token)


@contextmanager
def correlation(factory_run_id):
    token = _context.set({**_context.get(), "factory_run_id": factory_run_id})
    try:
        yield
    finally:
        _context.reset(token)


def configure_logging(settings):
    handler = logging.StreamHandler()
    handler.addFilter(CorrelationFilter())
    handler.setFormatter(
        JsonFormatter()
        if settings.log_format == "json"
        else logging.Formatter("%(levelname)s %(name)s %(message)s")
    )
    logging.basicConfig(
        level=settings.log_level.upper(), handlers=[handler], force=True
    )


def logging_observer(settings):
    return LoggingObserver()


def current_metadata():
    return dict(_context.get())
